"""本体查询动作（REST 与 MCP 共用的唯一实现）。

为什么放在 service 层：`/api/v1/ontology/*` 和 MCP 工具是同一批查询的两种入口。
逻辑只写一遍，两边就不会随时间漂移。这里抛 `BizError`（与 services 层既有约定一致），
HTTP 层交给全局异常处理器，MCP 层翻译成结构化错误。

| 函数 | 对应接口 | 对应 MCP 工具 |
|---|---|---|
| `search_entities` | GET  /v1/ontology/search        | ontology_search |
| `entity_detail`   | GET  /v1/ontology/entities/{uid}| ontology_entity |
| `graph_neighbors` | GET  /v1/ontology/neighbors/{uid}| ontology_neighbors |
| `param_cards`     | GET  /v1/ontology/params        | get_parameter_priors |

约定：
- 只读，不写库、不写审计；
- 找不到 → `BizError(404)`；图库未启用 → `BizError(503, GRAPH_DISABLED)`；
- 返回值都是可 JSON 序列化的 dict，调用方不再做二次转换。
"""
from __future__ import annotations

from typing import Any, Iterable

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import BizError
from app.db.models import OntologyInstance
from app.services.graph.ontology import instances as inst_mod
from app.services.graph.ontology.cards import recommend
from app.services.graph.ontology.linking import link_graph_entities, link_instances
from app.services.graph.ontology.registry import get_registry, OntologyRegistry

# 实体链接要扫全部实例（参数总量是几十条，不是几十万条）
_SEARCH_SCAN_LIMIT = 500


def resolve_profile(profile: str | None) -> OntologyRegistry:
    """取 TBox。profile 写错是用户可修复的问题 → 400，不是 500。"""
    wanted = (profile or settings.ontology_profile or "").strip()
    if not wanted:
        raise BizError(
            "未配置本体 profile（ONTOLOGY_PROFILE）", 400, "ONTOLOGY_PROFILE_MISSING"
        )
    try:
        return get_registry(wanted)
    except Exception as exc:  # noqa: BLE001 - YAML 写错要给可读提示
        raise BizError(
            f"本体 profile 加载失败：{exc}", 400, "ONTOLOGY_PROFILE_INVALID"
        ) from exc


def instance_to_dict(row: OntologyInstance) -> dict[str, Any]:
    """实例 → 可序列化 dict（字段与 InstanceOut 一致，不依赖 pydantic）。"""
    return {
        "id": row.id,
        "uid": row.uid,
        "profile_id": row.profile_id,
        "entity_type": row.entity_type,
        "name": row.name,
        "kb_id": row.kb_id,
        "props": dict(row.props or {}),
        "ranges": row.ranges,
        "evidence": row.evidence,
        "source": row.source,
        "status": row.status,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


async def search_entities(
    db: AsyncSession,
    q: str,
    *,
    profile_id: str | None = None,
    kb_id: int | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """实体链接：问题里的词 → 本体实例 / 图节点，按匹配度倒序。"""
    reg = resolve_profile(profile_id)
    rows = await inst_mod.list_instances(
        db, profile_id=reg.profile_id, status="active", limit=_SEARCH_SCAN_LIMIT
    )
    hits = link_instances(rows, q)
    hits += await link_graph_entities(db, q, kb_id=kb_id, limit=limit)
    hits.sort(key=lambda x: -x.score)
    return [h.to_dict() for h in hits[:limit]]


async def entity_detail(
    db: AsyncSession,
    uid: str,
    *,
    depth: int = 1,
    include_evidence: bool = True,
) -> dict[str, Any]:
    """实体详情 + 1~2 跳邻居。本体实例优先，找不到再退回图节点。"""
    from app.services.graph.store import get_graph_store

    row = await inst_mod.get_instance_by_uid(db, uid)
    store = get_graph_store()
    node = await store.get_node(uid) if store is not None else None
    if row is None and node is None:
        raise BizError("实体不存在", 404, "ONTOLOGY_ENTITY_NOT_FOUND")

    detail: dict[str, Any] = {
        "uid": uid,
        "instance": instance_to_dict(row) if row is not None else None,
        "node": (
            {"name": node.name, "entity_type": node.type_name, "props": node.props}
            if node is not None
            else None
        ),
    }
    if include_evidence and row is not None:
        detail["evidence"] = row.evidence or []
    if store is not None:
        # 详情接口沿用 neighbors 字段名（历史契约，不改），纯图接口才叫 nodes
        detail.update(
            _subgraph_payload(await store.neighbors(uid, depth=depth), nodes_key="neighbors")
        )
    return detail


async def graph_neighbors(
    uid: str, *, depth: int = 1, limit: int = 100
) -> dict[str, Any]:
    """纯图查询：某个节点周围 1~2 跳的子图。图库没开就明确报 503，不给空结果。"""
    from app.services.graph.store import get_graph_store

    store = get_graph_store()
    if store is None:
        raise BizError("图库未启用（GRAPH_ENABLED=false）", 503, "GRAPH_DISABLED")
    slice_ = await store.neighbors(uid, depth=depth, limit=limit)
    payload = {"uid": uid}
    payload.update(_subgraph_payload(slice_, with_props=True))
    payload["truncated"] = slice_.truncated
    return payload


def _subgraph_payload(
    slice_: Any, *, with_props: bool = False, nodes_key: str = "nodes"
) -> dict[str, Any]:
    names = {n.uid: n.name for n in slice_.nodes}
    nodes = []
    for n in slice_.nodes:
        item = {"uid": n.uid, "name": n.name, "entity_type": n.type_name}
        if with_props:
            item["props"] = n.props
        nodes.append(item)
    edges = []
    for e in slice_.edges:
        item = {
            "subject": names.get(e.subject_uid, e.subject_uid),
            "predicate": e.predicate_name,
            "object": names.get(e.object_uid, e.object_uid),
            "evidence_text": e.evidence_text,
            "chunk_ids": e.chunk_ids,
        }
        if with_props:
            item.update({"status": e.status, "confidence": e.confidence})
        edges.append(item)
    return {nodes_key: nodes, "edges": edges}


async def param_cards(
    db: AsyncSession,
    *,
    profile_id: str | None = None,
    names: Iterable[str] | None = None,
    group: str | None = None,
    kb_id: int = 0,
    climate: str | None = None,
    flood_type: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """参数推荐卡：条件收窄 → 冲突并列 → 无依据不出数。"""
    reg = resolve_profile(profile_id)
    cards = await recommend(
        db,
        profile_id=reg.profile_id,
        names=names,
        conditions={"climate": climate, "flood_type": flood_type},
        kb_id=kb_id,
        limit=limit,
    )
    if group:
        cards = [c for c in cards if c.group == group]
    return [card.to_dict() for card in cards]
