"""F5 图提案的执行层（C-2 按文档整体扫）。

分工：`proposals.py` 只管「一条提案合不合本体」（纯函数，好测）；这里管
「读哪些切片、调几次模型、跨批次怎么串」——要连库、连模型、要控成本。

**不假装扫全了**

一份 400 页的报告有上千个切片，一次全扫既慢又贵。这里按实体类型的中文名给
切片打分，有限的预算先扫最可能有东西的段落，并把「范围内共 N 段，本次只扫了
M 段」如实报出去。**没送进模型的那部分就是没扫**，不说清楚，用户会以为本体
已经建全了——这正是这套系统最不该犯的错。

**跨批次要记得住**

一批只送 4 段，同一座水库的实体常散落在好几批里。前一批提过的实例要并进
查找表，后一批的关系才能连过去；重复提的按 `origin="run"` 报「本次运行已经
提过了」，而不是让人去库里找一个还没入库的东西。
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Chunk, Document, OntologyInstance
from app.services.graph.ontology.anchoring import Candidate
from app.services.graph.ontology.proposals import (
    SYSTEM_PROMPT,
    InstanceCandidate,
    Known,
    ProposalRunResult,
    RelationCandidate,
    _clean_name,
    build_candidates,
    build_scan_prompt,
    known_from_instances,
    parse_payload,
)
from app.services.graph.ontology.registry import OntologyRegistry

logger = logging.getLogger(__name__)

_MIN_CHUNK_CHARS = 80        # 太短的切片（页眉、目录条目）不值得花一次调用
_DEFAULT_MAX_CHUNKS = 80     # 一次运行最多扫多少段
_DEFAULT_BATCH = 4           # 一次模型调用送几段
_DEFAULT_MAX_CALLS = 10      # 一次运行最多调几次模型（花钱的闸）


# 封面/目录/签署页：一次模型调用的预算换不到任何实例，还**特别容易得高分**
# （目录里列着全部章节标题，实体名一个不落）。实测 80 段里有 4 段是这种。
_FRONT_MATTER_SECTIONS = ("目录", "前言", "封面", "编制说明")
_FRONT_MATTER_PENALTY = -100


def is_front_matter(content: str, section: str | None = None) -> bool:
    """这一段是不是封面/目录/签署页（不是没有价值的正文）。"""
    head = (section or "").strip()
    if any(head.startswith(word) or word in head for word in _FRONT_MATTER_SECTIONS):
        return True
    # 目录的典型长相：一堆 "章节名……页码"
    return (content or "").count("....") >= 5


def score_chunk(
    content: str, registry: OntologyRegistry, *, section: str | None = None
) -> int:
    """按实体类型的中文名给切片打分：命中几个、名字多长。

    权重用名字长度，因为越长的名字越具体——「响应等级」比「工况」更能说明这段
    在讲什么。这只是**排序**，不是筛选：分数低的段在预算够的时候照样会扫，
    但封面/目录会被压到最后（见 `is_front_matter`）。
    """
    text = content or ""
    if is_front_matter(text, section):
        return _FRONT_MATTER_PENALTY
    score = 0
    for ent in registry.entity_types.values():
        for word in (ent.display_name, ent.name):
            if len(word) >= 2 and word in text:
                score += len(word)
    return score


async def _active_versions(
    db: AsyncSession, *, kb_id: int | None, doc_ids: list[int] | None
) -> set[int]:
    """当前 active 的文档版本（库里有 active + retired 两套切片，只扫 active）。"""
    from app.services.retrieval.scope import active_version_ids

    return await active_version_ids(db, kb_id=kb_id, doc_ids=doc_ids)


def _scope_filter(stmt, *, kb_id: int | None, doc_ids: list[int] | None, active: set[int]):
    if active:
        stmt = stmt.where(Chunk.document_version_id.in_(active))
    if doc_ids:
        stmt = stmt.where(Chunk.doc_id.in_(doc_ids))
    elif kb_id is not None:
        stmt = stmt.where(Chunk.kb_id == kb_id)
    return stmt


async def scan_chunks(
    db: AsyncSession,
    registry: OntologyRegistry,
    *,
    kb_id: int | None = None,
    doc_ids: list[int] | None = None,
    limit: int = _DEFAULT_MAX_CHUNKS,
) -> tuple[list[Candidate], int]:
    """挑要扫的切片。返回 (选中的切片, 范围内一共有多少段)。

    段数不多时按原文顺序全取（读起来连贯）；超预算才按相关度挑，挑完**仍按
    原文顺序排**——模型读乱序的段落，关系很容易连错。
    """
    active = await _active_versions(db, kb_id=kb_id, doc_ids=doc_ids)
    base = (
        select(Chunk.id)
        .join(Document, Chunk.doc_id == Document.id)
        .where(func.length(Chunk.content) >= _MIN_CHUNK_CHARS)
    )
    base = _scope_filter(base, kb_id=kb_id, doc_ids=doc_ids, active=active)
    total = int((await db.scalar(select(func.count()).select_from(base.subquery()))) or 0)
    if total == 0:
        return [], 0

    stmt = (
        select(Chunk.id, Chunk.content, Chunk.section)
        .join(Document, Chunk.doc_id == Document.id)
        .where(func.length(Chunk.content) >= _MIN_CHUNK_CHARS)
    )
    stmt = _scope_filter(stmt, kb_id=kb_id, doc_ids=doc_ids, active=active)
    scored = (await db.execute(stmt)).all()
    if len(scored) > limit:
        ranked = sorted(
            ((score_chunk(row[1] or "", registry, section=row[2]), row[0]) for row in scored),
            reverse=True,
        )
        keep = {cid for _, cid in ranked[:limit]}
    else:
        keep = {row[0] for row in scored}
    if not keep:
        return [], total

    rows = (
        await db.execute(
            select(
                Chunk.id,
                Chunk.doc_id,
                Chunk.page,
                Chunk.section,
                Chunk.content,
                Document.filename,
                Chunk.printed_page,
            )
            .join(Document, Chunk.doc_id == Document.id)
            .where(Chunk.id.in_(keep))
            .order_by(Chunk.doc_id, Chunk.chunk_index)
        )
    ).all()
    return [
        Candidate(
            chunk_id=int(r[0]),
            doc_id=int(r[1]),
            page=r[2],
            section=r[3],
            content=r[4] or "",
            doc_name=r[5] or "",
            printed_page=r[6],
        )
        for r in rows
    ], total


async def _resolve_kb_id(
    db: AsyncSession, kb_id: int | None, doc_ids: list[int] | None
) -> int:
    """提案里的 uid 要按库生成，所以得知道这批切片属于哪个库。

    只给了文档时反查一次；多库混扫时退回 0（全局），并在结果里说明——
    uid 落错库比少建一条更麻烦。
    """
    if kb_id is not None:
        return kb_id
    if not doc_ids:
        return 0
    rows = (
        await db.execute(select(Document.kb_id).where(Document.id.in_(doc_ids)).distinct())
    ).all()
    values = {r[0] for r in rows}
    return values.pop() if len(values) == 1 else 0


async def _call_llm(llm: Any, prompt: str) -> str:
    messages = [("system", SYSTEM_PROMPT), ("human", prompt)]
    resp = await llm.ainvoke(messages)
    content = getattr(resp, "content", "")
    return content if isinstance(content, str) else str(content)


async def run_document_scan(
    db: AsyncSession,
    registry: OntologyRegistry,
    *,
    kb_id: int | None = None,
    doc_ids: list[int] | None = None,
    max_chunks: int = _DEFAULT_MAX_CHUNKS,
    batch_size: int = _DEFAULT_BATCH,
    max_calls: int = _DEFAULT_MAX_CALLS,
    llm: Any = None,
    result: ProposalRunResult | None = None,
) -> tuple[list[InstanceCandidate], list[RelationCandidate], ProposalRunResult]:
    """按文档整体扫：切片 → 模型提案 → 逐条硬校验 → 返回候选（不落库）。

    落库交给调用方（`proposals.save_proposals`），和 F4/P2 一致——预演模式
    因此不需要任何额外开关。
    """
    result = result or ProposalRunResult()
    chunks, total = await scan_chunks(
        db, registry, kb_id=kb_id, doc_ids=doc_ids, limit=max_chunks
    )
    result.chunks = total
    if not chunks:
        result.notes.append("范围内没有可扫的切片：这个库还没解析出内容，或文档太短")
        return [], [], result
    if total > len(chunks):
        result.notes.append(
            f"范围内共 {total} 段，本次按实体名命中只挑 {len(chunks)} 段送模型；"
            "没送的部分这一轮不会出现在提案里"
        )

    scope_kb = await _resolve_kb_id(db, kb_id, doc_ids)
    if kb_id is None and doc_ids and scope_kb == 0:
        result.notes.append("这批文档跨多个知识库，实例 uid 按全局生成，请核对后再接受")

    rows = (
        await db.scalars(
            select(OntologyInstance).where(
                OntologyInstance.profile_id == registry.profile_id,
                OntologyInstance.status == "active",
            )
        )
    ).all()
    by_name, by_uid = known_from_instances(rows)
    scope_name, scope_uid = dict(by_name), dict(by_uid)

    instances: list[InstanceCandidate] = []
    relations: list[RelationCandidate] = []
    calls = 0
    for start in range(0, len(chunks), batch_size):
        if calls >= max_calls:
            left = len(chunks) - start
            result.notes.append(f"模型调用到上限 {max_calls} 次，后面 {left} 段这一轮不扫")
            break
        batch = chunks[start : start + batch_size]
        if llm is None:
            from app.services.chat import build_chat_model

            llm = build_chat_model(0.0)  # 抽取要稳定，温度 0
        try:
            text = await _call_llm(llm, build_scan_prompt(registry, batch, scope_uid))
        except Exception as exc:  # noqa: BLE001 - 单批失败不该毁掉整轮
            logger.warning("图提案调用失败（第 %s 批）：%s", start // batch_size + 1, exc)
            result.notes.append(
                f"第 {start // batch_size + 1} 批模型调用失败（{exc.__class__.__name__}），跳过"
            )
            calls += 1
            continue
        calls += 1
        result.scanned += len(batch)

        inst_items, rel_items = parse_payload(text)
        got_i, got_r = build_candidates(
            inst_items,
            rel_items,
            batch,
            registry=registry,
            by_name=scope_name,
            by_uid=scope_uid,
            kb_id=scope_kb,
            result=result,
        )
        instances.extend(got_i)
        relations.extend(got_r)
        for cand in got_i:
            known = Known(
                uid=cand.uid, entity_type=cand.entity_type, name=cand.name, origin="run"
            )
            scope_uid[cand.uid] = known
            scope_name.setdefault(_clean_name(cand.name), known)

    result.instances = len(instances)
    result.relations = len(relations)
    return instances, relations, result
