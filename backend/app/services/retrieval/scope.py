"""检索作用域查询（G6-2 从 rag.py 抽出）。

- active 文档版本 id：DB 同时存 active+retired，直接查 DB 的路径必须过滤 retired
- active 向量 collection 名：按 kb 的 active_index_version_id 路由（软切换）
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Document, IndexVersion, KnowledgeBase


async def active_version_ids(
    db: AsyncSession, kb_id: int | None = None, doc_ids: list[int] | None = None
) -> set[int]:
    """当前 active 的文档版本 id 集合（检索 DB 直接查询时过滤 retired 用）。

    P0-8：DB 同时存 active + retired 版本的 chunks；向量/BM25 索引只含 active，
    但**直接查 DB** 的路径（doc_chunk_ids 解析、章节/枚举扩展、整文档补全）必须
    显式过滤 retired，否则会把不可查的旧版切片混进证据。
    """
    stmt = select(Document.active_version_id).where(Document.active_version_id.is_not(None))
    if kb_id is not None:
        stmt = stmt.where(Document.kb_id == kb_id)
    if doc_ids:
        stmt = stmt.where(Document.id.in_(doc_ids))
    rows = (await db.execute(stmt)).all()
    return {r[0] for r in rows}


async def active_collection_names(db: AsyncSession, kb_id: int | None = None) -> list[str]:
    """当前 active 索引版本对应的 Chroma collection 名列表。

    P1-1/P1-2 软切换：检索不再读单一 collection，而是按 kb 的
    active_index_version_id → IndexVersion.physical_name 路由到对应 collection。
    kb_id 给定 → 该库一个名字；kb_id=None → 所有库的 active collection 名（跨库检索）。
    指针为 NULL（无索引）的库跳过。
    """
    stmt = (
        select(IndexVersion.physical_name)
        .join(KnowledgeBase, KnowledgeBase.active_index_version_id == IndexVersion.id)
    )
    if kb_id is not None:
        stmt = stmt.where(KnowledgeBase.id == kb_id)
    rows = (await db.execute(stmt)).scalars().all()
    return [r for r in rows if r]
