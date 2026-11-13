"""进程内图存储（InMemoryGraphStore）：写入合并、删除、邻居、搜索、统计。"""
from __future__ import annotations

import pytest

from app.services.graph.schema import Edge, EntityType, Node, RelationType, uid_for
from app.services.graph.store import InMemoryGraphStore


def _node(entity_type: EntityType, name: str, kb_id: int = 1, **props) -> Node:
    return Node(
        uid=uid_for(entity_type, name, kb_id),
        entity_type=entity_type,
        name=name,
        props=props,
        kb_ids=[kb_id],
    )


def _edge(subject: Node, predicate: RelationType, obj: Node, kb_id: int = 1, **kw) -> Edge:
    return Edge(
        subject_uid=subject.uid,
        predicate=predicate,
        object_uid=obj.uid,
        kb_id=kb_id,
        doc_id=kw.get("doc_id"),
        version_id=kw.get("version_id"),
        chunk_ids=kw.get("chunk_ids", []),
        status=kw.get("status", "auto"),
        confidence=kw.get("confidence", 1.0),
    )


@pytest.fixture
def store() -> InMemoryGraphStore:
    return InMemoryGraphStore()


async def _seed(store: InMemoryGraphStore) -> dict[str, Node]:
    reservoir = _node(EntityType.ENGINEERING_OBJECT, "鲤鱼塘水库")
    reach = _node(EntityType.RIVER, "下游河段1")
    town = _node(EntityType.ADMIN_REGION, "XX镇")
    measure = _node(EntityType.MEASURE, "XX镇·预警发布")
    await store.upsert(
        [reservoir, reach, town, measure],
        [
            _edge(reservoir, RelationType.DOWNSTREAM_OF, reach, chunk_ids=[101]),
            _edge(reach, RelationType.DOWNSTREAM_OF, town, chunk_ids=[102]),
            _edge(town, RelationType.NEEDS_WARNING, measure, chunk_ids=[103]),
        ],
    )
    return {"reservoir": reservoir, "reach": reach, "town": town, "measure": measure}


class TestUpsert:
    async def test_merges_duplicate_edges(self, store):
        reservoir = _node(EntityType.ENGINEERING_OBJECT, "鲤鱼塘水库")
        reach = _node(EntityType.RIVER, "下游河段1")
        await store.upsert([reservoir, reach], [_edge(reservoir, RelationType.DOWNSTREAM_OF, reach, chunk_ids=[1])])
        await store.upsert([], [_edge(reservoir, RelationType.DOWNSTREAM_OF, reach, chunk_ids=[2], confidence=0.8)])

        stats = await store.stats(kb_id=1)
        assert stats["edges"]["total"] == 1
        slice_ = await store.neighbors(reservoir.uid)
        assert slice_.edges[0].chunk_ids == [1, 2]

    async def test_rejected_status_wins_over_auto(self, store):
        reservoir = _node(EntityType.ENGINEERING_OBJECT, "鲤鱼塘水库")
        reach = _node(EntityType.RIVER, "下游河段1")
        await store.upsert([reservoir, reach], [_edge(reservoir, RelationType.DOWNSTREAM_OF, reach, status="auto")])
        await store.upsert([], [_edge(reservoir, RelationType.DOWNSTREAM_OF, reach, status="rejected")])
        slice_ = await store.neighbors(reservoir.uid)
        assert slice_.edges == []  # 被驳回的边不参与查询


class TestDelete:
    async def test_delete_version(self, store):
        nodes = await _seed(store)
        assert (await store.stats())["edges"]["total"] == 3
        reservoir, reach = nodes["reservoir"], nodes["reach"]
        await store.upsert([], [_edge(reservoir, RelationType.DOWNSTREAM_OF, reach, doc_id=7, version_id=3, chunk_ids=[9])])
        await store.delete_version(doc_id=7, version_id=3)
        slice_ = await store.neighbors(reservoir.uid)
        assert all(e.version_id != 3 for e in slice_.edges)

    async def test_delete_document(self, store):
        reservoir = _node(EntityType.ENGINEERING_OBJECT, "鲤鱼塘水库")
        reach = _node(EntityType.RIVER, "下游河段1")
        await store.upsert([reservoir, reach], [_edge(reservoir, RelationType.DOWNSTREAM_OF, reach, doc_id=5, version_id=1)])
        await store.delete_document(5)
        assert (await store.stats())["edges"]["total"] == 0

    async def test_delete_kb(self, store):
        await _seed(store)
        await store.delete_kb(1)
        stats = await store.stats()
        assert stats["nodes"]["total"] == 0
        assert stats["edges"]["total"] == 0


class TestQuery:
    async def test_neighbors_depth_1(self, store):
        nodes = await _seed(store)
        slice_ = await store.neighbors(nodes["reach"].uid, depth=1)
        names = {n.name for n in slice_.nodes}
        assert names == {"鲤鱼塘水库", "下游河段1", "XX镇"}

    async def test_neighbors_depth_2(self, store):
        """水库 →(1跳) 河段 →(2跳) 乡镇；措施在第 3 跳，不应出现。"""
        nodes = await _seed(store)
        slice_ = await store.neighbors(nodes["reservoir"].uid, depth=2)
        names = {n.name for n in slice_.nodes}
        assert "XX镇" in names
        assert "XX镇·预警发布" not in names

    async def test_neighbors_unknown_uid(self, store):
        slice_ = await store.neighbors("eo:not-exist")
        assert slice_.nodes == [] and slice_.edges == []

    async def test_rel_type_filter(self, store):
        nodes = await _seed(store)
        slice_ = await store.neighbors(nodes["town"].uid, rel_types=[RelationType.NEEDS_WARNING])
        # 关系名落库时统一成大写蛇形（见 schema.canonical_relation）
        assert {e.predicate for e in slice_.edges} == {"NEEDS_WARNING"}

    async def test_search_exact_and_partial(self, store):
        await _seed(store)
        exact = await store.search("鲤鱼塘水库", kb_id=1)
        assert exact and exact[0].name == "鲤鱼塘水库"
        partial = await store.search("下游", kb_id=1)
        assert any(n.name == "下游河段1" for n in partial)
        assert await store.search("不存在的东西", kb_id=1) == []

    async def test_search_kb_scope(self, store):
        await _seed(store)
        assert await store.search("鲤鱼塘水库", kb_id=2) == []

    async def test_stats_by_type(self, store):
        await _seed(store)
        stats = await store.stats(kb_id=1)
        assert stats["nodes"]["by_type"]["engineering_object"] == 1
        assert stats["edges"]["by_type"]["DOWNSTREAM_OF"] == 2


class TestOverview:
    """全览（前端画图用）：只返回两端都在结果里的边，超限要标 truncated。"""

    async def test_returns_all_nodes_and_edges(self, store):
        await _seed(store)
        slice_ = await store.overview(node_limit=100, edge_limit=100)
        assert len(slice_.nodes) == 4
        assert len(slice_.edges) == 3  # 水库→河段、河段→乡镇、乡镇→措施
        assert slice_.truncated is False
        uids = {n.uid for n in slice_.nodes}
        assert all(e.subject_uid in uids and e.object_uid in uids for e in slice_.edges)

    async def test_node_limit_truncates_and_drops_dangling_edges(self, store):
        await _seed(store)
        slice_ = await store.overview(node_limit=1, edge_limit=100)
        assert len(slice_.nodes) == 1
        assert slice_.truncated is True
        # 只有一个节点，边两端不可能都在结果里
        assert slice_.edges == []

    async def test_rejected_edges_excluded(self, store):
        nodes = await _seed(store)
        await store.upsert(
            [],
            [_edge(nodes["reservoir"], RelationType.DOWNSTREAM_OF, nodes["town"], status="rejected")],
        )
        slice_ = await store.overview()
        assert all(e.status != "rejected" for e in slice_.edges)

    async def test_kb_scope(self, store):
        await _seed(store)
        assert (await store.overview(kb_id=2)).nodes == []
        assert len((await store.overview(kb_id=1)).nodes) == 4


class TestCanonicalRelationName:
    """内置关系是小写枚举值，TBox 是大写蛇形；存储层要统一，否则同一关系两种写法。"""

    def test_builtin_values_map_to_upper(self):
        from app.services.graph.schema import canonical_relation

        assert canonical_relation("downstream_of") == "DOWNSTREAM_OF"
        assert canonical_relation(RelationType.NEEDS_WARNING) == "NEEDS_WARNING"
        assert canonical_relation("PART_OF") == "PART_OF"

    def test_unknown_name_is_uppercased(self):
        from app.services.graph.schema import canonical_relation

        assert canonical_relation("custom_rel") == "CUSTOM_REL"

    async def test_neighbors_filter_accepts_enum_after_canonical_storage(self, store):
        """按枚举过滤仍要命中已按大写落库的边。"""
        nodes = await _seed(store)
        by_enum = await store.neighbors(nodes["town"].uid, rel_types=[RelationType.NEEDS_WARNING])
        by_upper = await store.neighbors(nodes["town"].uid, rel_types=["NEEDS_WARNING"])
        assert [e.predicate for e in by_enum.edges] == [e.predicate for e in by_upper.edges]
        assert by_enum.edges
