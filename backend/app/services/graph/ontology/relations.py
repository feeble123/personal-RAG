"""本体关系（ABox 的边）：PostgreSQL 是真相源，Neo4j 只是可重建的索引。

和 `instances.py` 完全同一套分工：
- 管理端的新建/修改/删除只写 `ontology_relations`；
- 需要看图时由本模块把关系重放成图里的边（`sync_to_graph`）。

为什么必须落 PG：关系以前只能靠 Excel 台账导入，而且只存在 Neo4j 里。
重建图 / 换机器一次，人工整理的关系就全没了，只能重来一遍。

证据同样适用「只增不减」：同一条关系可以被多份文献支撑，改动时不会把旧证据冲掉。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import OntologyRelation
from app.services.graph.schema import Edge, type_value

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RelationDraft:
    """一条待写入的关系（还没落库）。"""

    profile_id: str
    subject_uid: str
    predicate: str
    object_uid: str
    kb_id: int = 0
    props: dict[str, Any] = field(default_factory=dict)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    source: str = "manual"  # manual / import / extracted
    status: str = "active"  # active / archived

    def key(self) -> tuple[str, str, str, str, int]:
        return (self.profile_id, self.subject_uid, self.predicate, self.object_uid, self.kb_id)


async def get_relation(
    session: AsyncSession,
    profile_id: str,
    subject_uid: str,
    predicate: str,
    object_uid: str,
    kb_id: int = 0,
) -> OntologyRelation | None:
    stmt = select(OntologyRelation).where(
        OntologyRelation.profile_id == profile_id,
        OntologyRelation.subject_uid == subject_uid,
        OntologyRelation.predicate == predicate,
        OntologyRelation.object_uid == object_uid,
        OntologyRelation.kb_id == kb_id,
    )
    return (await session.execute(stmt)).scalars().first()


async def get_relation_by_id(session: AsyncSession, relation_id: int) -> OntologyRelation | None:
    return await session.get(OntologyRelation, relation_id)


async def list_relations(
    session: AsyncSession,
    *,
    profile_id: str | None = None,
    subject_uid: str | None = None,
    object_uid: str | None = None,
    predicate: str | None = None,
    kb_id: int | None = None,
    touch_uid: str | None = None,
    status: str | None = "active",
    limit: int = 2000,
) -> list[OntologyRelation]:
    """列关系。`touch_uid` 表示「主语或宾语是它」，用于按节点查关系。"""
    stmt = select(OntologyRelation)
    if profile_id:
        stmt = stmt.where(OntologyRelation.profile_id == profile_id)
    if subject_uid:
        stmt = stmt.where(OntologyRelation.subject_uid == subject_uid)
    if object_uid:
        stmt = stmt.where(OntologyRelation.object_uid == object_uid)
    if predicate:
        stmt = stmt.where(OntologyRelation.predicate == predicate)
    if kb_id is not None:
        stmt = stmt.where(OntologyRelation.kb_id == kb_id)
    if touch_uid:
        stmt = stmt.where(
            (OntologyRelation.subject_uid == touch_uid)
            | (OntologyRelation.object_uid == touch_uid)
        )
    if status:
        stmt = stmt.where(OntologyRelation.status == status)
    stmt = stmt.order_by(OntologyRelation.id).limit(limit)
    return list((await session.execute(stmt)).scalars().all())


def merge_evidence(existing: Iterable[Any], incoming: Iterable[Any]) -> list[Any]:
    """证据只增不减（按来源去重），与实例侧同一套规则。"""
    out = list(existing or [])
    seen = {
        (i.get("doc_id"), i.get("chunk_id"), i.get("type"), i.get("note"))
        if isinstance(i, dict)
        else (str(i),)
        for i in out
    }
    for item in incoming or []:
        key = (
            (item.get("doc_id"), item.get("chunk_id"), item.get("type"), item.get("note"))
            if isinstance(item, dict)
            else (str(item),)
        )
        if key not in seen:
            out.append(item)
            seen.add(key)
    return out


async def upsert_relation(session: AsyncSession, draft: RelationDraft) -> OntologyRelation:
    """按 (profile, 主语, 关系, 宾语, kb) 幂等写入。调用方负责 commit。"""
    existing = await get_relation(
        session,
        draft.profile_id,
        draft.subject_uid,
        draft.predicate,
        draft.object_uid,
        draft.kb_id,
    )
    if existing is None:
        row = OntologyRelation(
            profile_id=draft.profile_id,
            subject_uid=draft.subject_uid,
            predicate=draft.predicate,
            object_uid=draft.object_uid,
            kb_id=draft.kb_id,
            props=dict(draft.props),
            evidence=list(draft.evidence) or None,
            source=draft.source,
            status=draft.status,
        )
        session.add(row)
        await session.flush()
        return row

    existing.props = dict(draft.props)
    existing.evidence = merge_evidence(existing.evidence, draft.evidence) or None
    existing.source = draft.source
    existing.status = draft.status
    await session.flush()
    return existing


def apply_changes(
    row: OntologyRelation,
    *,
    predicate: str | None = None,
    props: dict[str, Any] | None = None,
    evidence: list[Any] | None = None,
    status: str | None = None,
    source: str | None = None,
) -> bool:
    """只改传进来的字段；返回是否真的有变化。"""
    changed = False
    if predicate is not None and predicate != row.predicate:
        row.predicate = predicate
        changed = True
    if props is not None and props != (row.props or {}):
        row.props = dict(props)
        changed = True
    if evidence is not None:
        merged = merge_evidence(row.evidence, evidence)
        if merged != (row.evidence or []):
            row.evidence = merged or None
            changed = True
    if status is not None and status != row.status:
        row.status = status
        changed = True
    if source is not None and source != row.source:
        row.source = source
        changed = True
    return changed


async def delete_relation(session: AsyncSession, row: OntologyRelation) -> None:
    await session.delete(row)
    await session.flush()


async def delete_relations_for_instances(session: AsyncSession, uids: Iterable[str]) -> int:
    """删实例时把挂在它身上的关系一起删掉，避免留下悬空边。"""
    targets = [u for u in uids if u]
    if not targets:
        return 0
    stmt = delete(OntologyRelation).where(
        (OntologyRelation.subject_uid.in_(targets))
        | (OntologyRelation.object_uid.in_(targets))
    )
    result = await session.execute(stmt)
    return int(result.rowcount or 0)


def to_edge(row: OntologyRelation) -> Edge:
    """关系 → 图里的边。证据里的 chunk_id 带进边，检索时能点回原文。"""
    evidence = [e for e in (row.evidence or []) if isinstance(e, dict)]
    chunk_ids = [int(e["chunk_id"]) for e in evidence if e.get("chunk_id") is not None]
    texts = [str(e.get("note") or e.get("snippet") or "") for e in evidence]
    return Edge(
        subject_uid=row.subject_uid,
        predicate=row.predicate,
        object_uid=row.object_uid,
        kb_id=row.kb_id,
        chunk_ids=chunk_ids,
        props=dict(row.props or {}),
        extractor="manual" if row.source == "manual" else "rule",
        status="auto" if row.status == "active" else "rejected",
        evidence_text="；".join(t for t in texts if t),
    )


async def sync_to_graph(rows: Iterable[OntologyRelation], store: Any) -> int:
    """把关系重放成图里的边。图库写失败是静默降级的，所以要抽查一次。"""
    if store is None:
        return 0
    edges = [to_edge(row) for row in rows]
    if not edges:
        return 0
    await store.upsert([], edges)
    first = edges[0]
    slice_ = await store.neighbors(first.subject_uid, depth=1, limit=200)
    hit = any(
        e.subject_uid == first.subject_uid
        and e.object_uid == first.object_uid
        and e.predicate_name == type_value(first.predicate)
        for e in slice_.edges
    )
    if not hit:
        return 0
    return len(edges)
