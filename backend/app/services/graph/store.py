"""图存储抽象 + 进程内实现（第一期 U1）。

设计要点：
- 只用协议（Protocol）约束能力，Neo4j 实现见 `neo4j_store.py`，业务代码不需要改；
- `get_graph_store()` 在 `GRAPH_ENABLED=false` 时返回 None —— 主链路必须先判空，
  这样「关掉图谱」就是真正的零成本；
- 进程内实现用于离线测试、dry-run 和本机试用（重启即丢，符合「图是派生索引」的定位）。
"""
from __future__ import annotations

import logging
from typing import Protocol

from app.core.config import settings
from app.services.graph.schema import (
    Edge,
    GraphSlice,
    Node,
    RelationType,
    canonical_relation,
    type_value,
)

logger = logging.getLogger(__name__)


class GraphStore(Protocol):
    """图存储能力契约（内存/Neo4j 都要实现）。"""

    async def upsert(self, nodes: list[Node], edges: list[Edge]) -> None: ...

    async def delete_version(self, doc_id: int, version_id: int) -> None: ...

    async def delete_document(self, doc_id: int) -> None: ...

    async def delete_kb(self, kb_id: int) -> None: ...

    async def delete_nodes(self, uids: list[str]) -> None: ...

    async def delete_edges(
        self, subject_uid: str, predicate: str, object_uid: str
    ) -> None: ...

    async def get_node(self, uid: str) -> Node | None: ...

    async def neighbors(
        self,
        uid: str,
        depth: int = 1,
        rel_types: list[RelationType] | None = None,
        limit: int = 200,
    ) -> GraphSlice: ...

    async def search(self, query: str, kb_id: int | None = None, limit: int = 50) -> list[Node]: ...

    async def overview(
        self,
        *,
        entity_types: list[str] | None = None,
        kb_id: int | None = None,
        node_limit: int = 500,
        edge_limit: int = 1500,
    ) -> GraphSlice: ...

    async def stats(self, kb_id: int | None = None) -> dict: ...

    async def health(self) -> bool: ...

    async def close(self) -> None: ...


class InMemoryGraphStore:
    """进程内图存储：测试、dry-run、本机试用。

    边按 (subject, predicate, object, version_id) 去重；同一版本内多条证据合并到一条边。
    """

    def __init__(self) -> None:
        self._nodes: dict[str, Node] = {}
        # key: (subject_uid, predicate_value, object_uid, version_id)
        self._edges: dict[tuple[str, str, str, int | None], Edge] = {}

    # ---------- 写入 ----------
    async def upsert(self, nodes: list[Node], edges: list[Edge]) -> None:
        for node in nodes:
            self._merge_node(node)
        for edge in edges:
            self._merge_edge(edge)

    def _merge_node(self, incoming: Node) -> None:
        existing = self._nodes.get(incoming.uid)
        if existing is None:
            self._nodes[incoming.uid] = Node(
                uid=incoming.uid,
                entity_type=incoming.entity_type,
                name=incoming.name,
                props=dict(incoming.props),
                aliases=list(dict.fromkeys(incoming.aliases)),
                kb_ids=list(dict.fromkeys(incoming.kb_ids)),
                doc_ids=list(dict.fromkeys(incoming.doc_ids)),
                version_ids=list(dict.fromkeys(incoming.version_ids)),
            )
            return
        # 已有节点：非空属性优先保留（人工/先到者），列表取并集
        for k, v in incoming.props.items():
            if v not in (None, "", []) or k not in existing.props:
                existing.props.setdefault(k, v)
        for field_name in ("aliases", "kb_ids", "doc_ids", "version_ids"):
            merged = list(getattr(existing, field_name))
            for item in getattr(incoming, field_name):
                if item not in merged:
                    merged.append(item)
            setattr(existing, field_name, merged)

    def _merge_edge(self, incoming: Edge) -> None:
        # 关系名统一成大写蛇形再落表：老数据是 downstream_of，TBox 是 DOWNSTREAM_OF，
        # 不归一的话同一条关系会因为大小写不同被当成两条。
        predicate = canonical_relation(incoming.predicate)
        key = (
            incoming.subject_uid,
            predicate,
            incoming.object_uid,
            incoming.version_id,
        )
        existing = self._edges.get(key)
        if existing is None:
            self._edges[key] = Edge(
                subject_uid=incoming.subject_uid,
                predicate=predicate,
                object_uid=incoming.object_uid,
                kb_id=incoming.kb_id,
                doc_id=incoming.doc_id,
                version_id=incoming.version_id,
                chunk_ids=list(dict.fromkeys(incoming.chunk_ids)),
                evidence_ids=list(dict.fromkeys(incoming.evidence_ids)),
                props=dict(incoming.props),
                extractor=incoming.extractor,
                confidence=incoming.confidence,
                status=incoming.status,
                evidence_text=incoming.evidence_text,
            )
            return
        for cid in incoming.chunk_ids:
            if cid not in existing.chunk_ids:
                existing.chunk_ids.append(cid)
        for eid in incoming.evidence_ids:
            if eid not in existing.evidence_ids:
                existing.evidence_ids.append(eid)
        # 人工结论优先于自动抽取：确认 > 驳回 > 自动。
        # 驳回必须能生效，否则「人工纠错」形同虚设；之后再人工确认可覆盖驳回。
        rank = {"auto": 0, "rejected": 1, "confirmed": 2}
        if rank.get(incoming.status, 1) > rank.get(existing.status, 1):
            existing.status = incoming.status
        existing.confidence = max(existing.confidence, incoming.confidence)
        for k, v in incoming.props.items():
            existing.props.setdefault(k, v)

    async def delete_version(self, doc_id: int, version_id: int) -> None:
        for key, edge in list(self._edges.items()):
            if edge.doc_id == doc_id and edge.version_id == version_id:
                del self._edges[key]

    async def delete_document(self, doc_id: int) -> None:
        for key, edge in list(self._edges.items()):
            if edge.doc_id == doc_id:
                del self._edges[key]

    async def delete_kb(self, kb_id: int) -> None:
        for key, edge in list(self._edges.items()):
            if edge.kb_id == kb_id:
                del self._edges[key]
        for uid, node in list(self._nodes.items()):
            if kb_id in node.kb_ids:
                del self._nodes[uid]

    async def delete_nodes(self, uids: list[str]) -> None:
        """删节点，连同挂在它身上的边（避免留下悬空边）。"""
        targets = set(uids)
        for uid in targets:
            self._nodes.pop(uid, None)
        for key, edge in list(self._edges.items()):
            if edge.subject_uid in targets or edge.object_uid in targets:
                del self._edges[key]

    async def delete_edges(self, subject_uid: str, predicate: str, object_uid: str) -> None:
        wanted = canonical_relation(predicate)
        for key, edge in list(self._edges.items()):
            if (
                edge.subject_uid == subject_uid
                and edge.object_uid == object_uid
                and canonical_relation(edge.predicate) == wanted
            ):
                del self._edges[key]

    # ---------- 读取 ----------
    async def get_node(self, uid: str) -> Node | None:
        return self._nodes.get(uid)

    def _visible_edges(self, rel_types: list[RelationType] | None = None) -> list[Edge]:
        out = []
        for edge in self._edges.values():
            if edge.status == "rejected":
                continue
            if rel_types and canonical_relation(edge.predicate) not in {
                canonical_relation(r) for r in rel_types
            }:
                continue
            out.append(edge)
        return out

    async def neighbors(
        self,
        uid: str,
        depth: int = 1,
        rel_types: list[RelationType] | None = None,
        limit: int = 200,
    ) -> GraphSlice:
        if uid not in self._nodes:
            return GraphSlice()
        max_depth = max(1, min(int(depth), 2))  # 只允许 1~2 跳，防爆炸
        seen_edges: dict[tuple, Edge] = {}
        frontier = {uid}
        visited = {uid}
        truncated = False

        for _ in range(max_depth):
            next_frontier: set[str] = set()
            for edge in self._visible_edges(rel_types):
                if edge.subject_uid in frontier or edge.object_uid in frontier:
                    if len(seen_edges) >= limit:
                        truncated = True
                        break
                    seen_edges[(edge.subject_uid, type_value(edge.predicate), edge.object_uid, edge.version_id)] = edge
                    for endpoint in (edge.subject_uid, edge.object_uid):
                        if endpoint not in visited:
                            visited.add(endpoint)
                            next_frontier.add(endpoint)
            frontier = next_frontier
            if not frontier:
                break

        nodes = [self._nodes[u] for u in visited if u in self._nodes]
        return GraphSlice(nodes=nodes, edges=list(seen_edges.values()), truncated=truncated)

    async def search(self, query: str, kb_id: int | None = None, limit: int = 50) -> list[Node]:
        from app.services.graph.schema import normalize_name

        q = normalize_name(query)
        if not q:
            return []
        hits: list[tuple[int, Node]] = []
        for node in self._nodes.values():
            if kb_id is not None and kb_id not in node.kb_ids:
                continue
            names = [node.name, *node.aliases]
            if any(q == normalize_name(n) for n in names):
                hits.append((0, node))
            elif any(q in normalize_name(n) for n in names):
                hits.append((1, node))
        hits.sort(key=lambda x: (x[0], -len(x[1].name)))
        return [n for _, n in hits[:limit]]

    async def overview(
        self,
        *,
        entity_types: list[str] | None = None,
        kb_id: int | None = None,
        node_limit: int = 500,
        edge_limit: int = 1500,
    ) -> GraphSlice:
        """全览：一次取回一批节点和边（给前端画图用，不等于全库导出）。

        边只保留两端都在本次结果里的，避免画出「悬空边」。
        """
        wanted = {type_value(t) for t in entity_types} if entity_types else None
        nodes = [
            n
            for n in self._nodes.values()
            if (not wanted or type_value(n.entity_type) in wanted)
            and (kb_id is None or kb_id in n.kb_ids)
        ]
        truncated = len(nodes) > node_limit
        nodes = nodes[: max(1, int(node_limit))]

        keep = {n.uid for n in nodes}
        edges = [
            e
            for e in self._visible_edges()
            if e.subject_uid in keep
            and e.object_uid in keep
            and (kb_id is None or e.kb_id == kb_id)
        ]
        if len(edges) > edge_limit:
            truncated = True
            edges = edges[: max(1, int(edge_limit))]
        return GraphSlice(nodes=nodes, edges=edges, truncated=truncated)

    async def stats(self, kb_id: int | None = None) -> dict:
        nodes = [n for n in self._nodes.values() if kb_id is None or kb_id in n.kb_ids]
        edges = [e for e in self._edges.values() if kb_id is None or e.kb_id == kb_id]
        by_type: dict[str, int] = {}
        for n in nodes:
            tv = type_value(n.entity_type)
            by_type[tv] = by_type.get(tv, 0) + 1
        by_rel: dict[str, int] = {}
        for e in edges:
            pred = type_value(e.predicate)
            by_rel[pred] = by_rel.get(pred, 0) + 1
        return {
            "nodes": {"total": len(nodes), "by_type": by_type},
            "edges": {"total": len(edges), "by_type": by_rel},
        }

    async def health(self) -> bool:
        return True

    async def close(self) -> None:
        return None


# ---------------- 工厂 ----------------

_store: GraphStore | None = None


def get_graph_store() -> GraphStore | None:
    """按配置返回图存储；`GRAPH_ENABLED=false` 时返回 None（调用方据此跳过）。"""
    global _store
    if not settings.graph_enabled:
        return None
    if _store is not None:
        return _store

    backend = (settings.graph_backend or "memory").strip().lower()
    if backend == "memory":
        _store = InMemoryGraphStore()
        logger.info("图谱存储：进程内内存实现（重启即丢，适合测试/试用）")
        return _store

    if backend == "neo4j":
        # 构造时不连接（懒加载），图库没起来也不会让进程启动失败
        from app.services.graph.neo4j_store import Neo4jGraphStore

        _store = Neo4jGraphStore()
        logger.info("图谱存储：Neo4j（%s）", settings.graph_neo4j_uri)
        return _store

    raise ValueError(f"未知的 GRAPH_BACKEND: {settings.graph_backend!r}")


def reset_graph_store() -> None:
    """测试用：清空单例，避免用例之间串数据。"""
    global _store
    _store = None
