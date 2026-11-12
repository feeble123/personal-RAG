"""Neo4j 图存储：行转换、降级、工厂接入。

**不连真库**：真库用例在 tests/integration/test_neo4j_store.py（需 NEO4J_TEST_URI）。
这里只验证「翻译得对不对」和「连不上时会不会拖垮主链路」。
"""
from __future__ import annotations

import json

import pytest

from app.core.config import settings
from app.services.graph import neo4j_store as nj
from app.services.graph.neo4j_store import (
    Neo4jGraphStore,
    _chunks,
    _edge_from_props,
    _edge_row,
    _lucene_escape,
    _node_from_props,
    _node_row,
    _scalar_props,
)
from app.services.graph.schema import Edge, Node
from app.services.graph.store import get_graph_store, reset_graph_store


def _node() -> Node:
    return Node(
        uid="pn:abc",
        entity_type="Parameter",
        name="sm",
        props={"group": "产流", "unit": "mm", "physical_low": 5, "tags": ["a"], "note": None},
        aliases=["自由水蓄量"],
        kb_ids=[1],
        doc_ids=[2],
        version_ids=[3],
    )


def _edge(**kw) -> Edge:
    return Edge(
        subject_uid="pn:abc",
        predicate="AFFECTS",
        object_uid="mc:def",
        kb_id=1,
        doc_id=kw.get("doc_id"),
        version_id=kw.get("version_id"),
        chunk_ids=kw.get("chunk_ids", [7]),
        status=kw.get("status", "auto"),
        confidence=kw.get("confidence", 0.8),
    )


class TestRowTranslation:
    def test_scalar_props_only_keeps_flat_values(self):
        scalars = _scalar_props({"group": "产流", "low": 5, "flag": True, "tags": ["a"], "note": None})
        assert scalars == {"p_group": "产流", "p_low": 5, "p_flag": True}

    def test_node_row_shape(self):
        row = _node_row(_node())
        assert row["uid"] == "pn:abc"
        assert row["entity_type"] == "Parameter"
        assert row["aliases_text"] == "sm 自由水蓄量"
        assert row["scalars"]["p_group"] == "产流"
        # 嵌套值不进图属性，但必须完整保留在 props_json 里（不丢数据）
        assert json.loads(row["props_json"])["tags"] == ["a"]

    def test_node_row_dedupes_name_in_aliases(self):
        node = _node()
        node.aliases = ["sm", "自由水蓄量"]
        row = _node_row(node)
        assert row["aliases"] == ["sm", "自由水蓄量"]

    def test_edge_row_version_sentinel(self):
        assert _edge_row(_edge(version_id=None))["ver"] == -1
        assert _edge_row(_edge(version_id=4))["ver"] == 4

    def test_edge_row_status_rank(self):
        assert _edge_row(_edge(status="auto"))["status_rank"] == 0
        assert _edge_row(_edge(status="rejected"))["status_rank"] == 1
        assert _edge_row(_edge(status="confirmed"))["status_rank"] == 2

    def test_node_roundtrip(self):
        node = _node_from_props(_node_row(_node()))
        assert node is not None
        assert node.uid == "pn:abc"
        assert node.props["group"] == "产流"
        assert node.props["tags"] == ["a"]
        assert node.aliases == ["sm", "自由水蓄量"]

    def test_edge_roundtrip_restores_none_version(self):
        """写入行 → Neo4j 存储属性 → 读回。写入键名与存储属性名不同，这里显式对齐。"""
        row = _edge_row(_edge(version_id=None))
        stored = dict(row)
        stored["relation_type"] = stored.pop("rel")
        stored["version_key"] = stored.pop("ver")
        edge = _edge_from_props("pn:abc", "mc:def", stored)
        assert edge is not None
        assert edge.version_id is None
        assert edge.predicate == "AFFECTS"
        assert edge.chunk_ids == [7]

    def test_edge_from_props_rejects_incomplete(self):
        assert _edge_from_props("", "mc:def", {}) is None
        assert _node_from_props({}) is None
        assert _node_from_props({"uid": ""}) is None

    def test_lucene_escape(self):
        assert _lucene_escape("sm:(1)") == "sm\\:\\(1\\)"
        assert _lucene_escape("自由水蓄量") == "自由水蓄量"

    def test_chunks(self):
        assert _chunks([]) == []
        assert len(_chunks(list(range(1201)), size=500)) == 3


class TestSliceAssembly:
    def test_dedupes_nodes_and_edges(self):
        node = _node_row(_node())
        other = dict(node, uid="mc:def", name="NSE", entity_type="Metric")
        edge = _edge_row(_edge(version_id=1))
        row = {
            "a": node,
            "b": other,
            "r": edge,
            "src": "pn:abc",
            "dst": "mc:def",
        }
        slice_ = Neo4jGraphStore._slice([row, row], (("r", "src", "dst"),), limit=10)
        assert {n.uid for n in slice_.nodes} == {"pn:abc", "mc:def"}
        assert len(slice_.edges) == 1
        assert slice_.truncated is False

    def test_truncated_when_limit_hit(self):
        node = _node_row(_node())
        rows = [
            {"a": node, "r": _edge_row(_edge(version_id=1)), "src": "pn:abc", "dst": "mc:def"}
        ]
        assert Neo4jGraphStore._slice(rows, (("r", "src", "dst"),), limit=1).truncated is True


class TestDegradation:
    """图库连不上时：不抛异常，返回空结果（主链路零影响）。"""

    @pytest.fixture
    def dead_store(self) -> Neo4jGraphStore:
        # 端口 1 一定连不上，连接立即被拒（不会真的等超时）
        return Neo4jGraphStore(
            uri="bolt://127.0.0.1:1",
            user="neo4j",
            password="x",
            query_timeout_s=1.0,
            connect_timeout_s=1.0,
        )

    async def test_upsert_does_not_raise(self, dead_store):
        await dead_store.upsert([_node()], [_edge()])

    async def test_queries_return_empty(self, dead_store):
        assert await dead_store.search("sm") == []
        assert await dead_store.get_node("pn:abc") is None
        slice_ = await dead_store.neighbors("pn:abc")
        assert slice_.nodes == [] and slice_.edges == []
        stats = await dead_store.stats()
        assert stats["nodes"]["total"] == 0 and stats["edges"]["total"] == 0

    async def test_delete_does_not_raise(self, dead_store):
        await dead_store.delete_version(1, 1)
        await dead_store.delete_document(1)
        await dead_store.delete_kb(1)

    async def test_health_false(self, dead_store):
        assert await dead_store.health() is False

    async def test_schema_init_fails_fast_without_retry_storm(self, dead_store):
        assert await dead_store.ensure_schema() is False
        # 冷却期内不再重试 DDL，直接秒返回
        await dead_store.upsert([_node()], [])


class TestFactory:
    def test_neo4j_backend_returns_store(self, monkeypatch):
        reset_graph_store()
        monkeypatch.setattr(settings, "graph_enabled", True)
        monkeypatch.setattr(settings, "graph_backend", "neo4j")
        monkeypatch.setattr(settings, "graph_neo4j_password", "secret")
        try:
            store = get_graph_store()
            assert isinstance(store, Neo4jGraphStore)
        finally:
            reset_graph_store()

    def test_construction_does_not_connect(self, monkeypatch):
        """构造不该建立连接：图库没启动时进程也要能起来。"""
        monkeypatch.setattr(settings, "graph_backend", "neo4j")
        store = Neo4jGraphStore(uri="bolt://127.0.0.1:1", password="x")
        assert store._driver_obj is None

    def test_node_label_constants(self):
        assert nj.NODE_LABEL == "OntologyEntity"
        assert nj.REL_TYPE == "ONT_REL"
