"""本体实例（ABox）：PostgreSQL 是真相源，Neo4j 只是可重建的索引。

为什么这样分工：
- 参数先验这类知识**靠人维护**。只写图库的话，重建图 / 换机器就全丢了；
- 所以 CRUD 只写 `ontology_instances`，需要看图时再由本模块把实例重放成图节点。

唯一键：(profile_id, entity_type, name, kb_id)。同一本体下同名同类型只有一条。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import OntologyInstance
from app.services.graph.schema import Node

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class InstanceDraft:
    """一条待写入的实例（还没落库）。"""

    profile_id: str
    entity_type: str
    name: str
    uid: str
    kb_id: int = 0
    props: dict[str, Any] = field(default_factory=dict)
    ranges: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    source: str = "manual"  # manual / import / extracted
    status: str = "active"  # active / archived

    def key(self) -> tuple[str, str, str, int]:
        return (self.profile_id, self.entity_type, self.name, self.kb_id)


@dataclass
class UpsertResult:
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.created + self.updated + self.unchanged

    def merge(self, other: "UpsertResult") -> "UpsertResult":
        return UpsertResult(
            created=self.created + other.created,
            updated=self.updated + other.updated,
            unchanged=self.unchanged + other.unchanged,
            errors=[*self.errors, *other.errors],
        )


async def get_instance(
    session: AsyncSession,
    profile_id: str,
    entity_type: str,
    name: str,
    kb_id: int = 0,
) -> OntologyInstance | None:
    stmt = select(OntologyInstance).where(
        OntologyInstance.profile_id == profile_id,
        OntologyInstance.entity_type == entity_type,
        OntologyInstance.name == name,
        OntologyInstance.kb_id == kb_id,
    )
    return (await session.execute(stmt)).scalars().first()


async def get_instance_by_id(session: AsyncSession, instance_id: int) -> OntologyInstance | None:
    stmt = select(OntologyInstance).where(OntologyInstance.id == instance_id)
    return (await session.execute(stmt)).scalars().first()


async def get_instance_by_uid(session: AsyncSession, uid: str) -> OntologyInstance | None:
    stmt = select(OntologyInstance).where(OntologyInstance.uid == uid)
    return (await session.execute(stmt)).scalars().first()


def apply_changes(
    row: OntologyInstance,
    *,
    props: dict[str, Any] | None = None,
    ranges: list[dict[str, Any]] | None = None,
    evidence: list[dict[str, Any]] | None = None,
    status: str | None = None,
    source: str | None = None,
) -> bool:
    """就地修改实例字段（未传的字段保持不动）；返回是否真的变了。"""
    changed = False
    if props is not None and dict(props) != (row.props or {}):
        row.props = dict(props)
        changed = True
    if ranges is not None and list(ranges) != (row.ranges or []):
        row.ranges = list(ranges) or None
        changed = True
    if evidence is not None and list(evidence) != (row.evidence or []):
        row.evidence = list(evidence) or None
        changed = True
    if status is not None and status != row.status:
        row.status = status
        changed = True
    if source is not None and source != row.source:
        row.source = source
        changed = True
    return changed


async def delete_instance(session: AsyncSession, row: OntologyInstance) -> None:
    await session.delete(row)


async def list_instances(
    session: AsyncSession,
    *,
    profile_id: str | None = None,
    entity_type: str | None = None,
    kb_id: int | None = None,
    status: str | None = "active",
    limit: int = 200,
) -> list[OntologyInstance]:
    stmt = select(OntologyInstance)
    if profile_id:
        stmt = stmt.where(OntologyInstance.profile_id == profile_id)
    if entity_type:
        stmt = stmt.where(OntologyInstance.entity_type == entity_type)
    if kb_id is not None:
        stmt = stmt.where(OntologyInstance.kb_id == kb_id)
    if status:
        stmt = stmt.where(OntologyInstance.status == status)
    stmt = stmt.order_by(OntologyInstance.entity_type, OntologyInstance.name).limit(limit)
    return list((await session.execute(stmt)).scalars().all())


def _differs(row: OntologyInstance, draft: InstanceDraft) -> bool:
    return (
        row.uid != draft.uid
        or (row.props or {}) != (draft.props or {})
        or (row.ranges or []) != (draft.ranges or [])
        or (row.evidence or []) != (draft.evidence or [])
        or row.source != draft.source
        or row.status != draft.status
    )


async def upsert_instances(session: AsyncSession, drafts: Iterable[InstanceDraft]) -> UpsertResult:
    """批量写入/更新实例（幂等）。调用方负责 commit。"""
    result = UpsertResult()
    for draft in drafts:
        existing = await get_instance(
            session, draft.profile_id, draft.entity_type, draft.name, draft.kb_id
        )
        if existing is None:
            session.add(
                OntologyInstance(
                    uid=draft.uid,
                    profile_id=draft.profile_id,
                    entity_type=draft.entity_type,
                    name=draft.name,
                    kb_id=draft.kb_id,
                    props=dict(draft.props),
                    ranges=list(draft.ranges) or None,
                    evidence=list(draft.evidence) or None,
                    source=draft.source,
                    status=draft.status,
                )
            )
            result.created += 1
            continue
        if not _differs(existing, draft):
            result.unchanged += 1
            continue
        existing.uid = draft.uid
        existing.props = dict(draft.props)
        existing.ranges = list(draft.ranges) or None
        existing.evidence = list(draft.evidence) or None
        existing.source = draft.source
        existing.status = draft.status
        result.updated += 1
    await session.flush()
    return result


def to_node(row: OntologyInstance) -> Node:
    """实例 → 图节点。列表型属性（ranges/evidence）不进节点标量，靠 props_json 保留。"""
    props: dict[str, Any] = {k: v for k, v in (row.props or {}).items() if v is not None}
    if row.ranges:
        props["range_count"] = len(row.ranges)
    if row.evidence:
        props["evidence_count"] = len(row.evidence)
    aliases = [props["display_zh"]] if props.get("display_zh") else []
    return Node(
        uid=row.uid,
        entity_type=row.entity_type,
        name=row.name,
        props=props,
        aliases=aliases,
        kb_ids=[row.kb_id] if row.kb_id else [0],
    )


async def sync_to_graph(rows: Iterable[OntologyInstance], store: Any) -> int:
    """把实例重放成图节点（图库是派生索引，随时可重建）。

    图库写入失败会被**静默降级**（这是主链路的要求），所以这里必须抽样验一次：
    否则「图库挂了」会被报成「同步成功 N 个节点」。
    返回真正写进去的节点数（0 表示没成功）。
    """
    if store is None:
        return 0
    nodes = [to_node(r) for r in rows]
    if not nodes:
        return 0
    await store.upsert(nodes, [])
    probe = await store.get_node(nodes[0].uid)
    return len(nodes) if probe is not None else 0
