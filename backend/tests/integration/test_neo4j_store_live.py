"""Neo4j 真库集成测试（默认跳过）。

跑法（本机装好 Neo4j Desktop 并启动数据库后）：

```powershell
$env:NEO4J_TEST_URI="bolt://localhost:7687"
$env:NEO4J_TEST_PASSWORD="<你设置的密码>"
python -m pytest tests/integration -q
```

只用一个专属 kb_id（9001）做数据隔离，跑完自动清理；不会碰真实知识库。
"""
from __future__ import annotations

import os

import pytest

from app.services.graph.neo4j_store import Neo4jGraphStore
from app.services.graph.schema import Edge, Node

NEO4J_URI = os.getenv("NEO4J_TEST_URI", "").strip()
pytestmark = pytest.mark.skipif(
    not NEO4J_URI, reason="未配置 NEO4J_TEST_URI，跳过 Neo4j 真库用例"
)

KB_ID = 9001  # 专属测试库 id，避免污染真实数据


def _node(entity_type: str, name: str, **props) -> Node:
    # 这里只测存储层，uid 直接用稳定字符串，不依赖 ontology profile 是否加载
    return Node(
        uid=f"test:{entity_type}:{name}",
        entity_type=entity_type,
        name=name,
        props=props,
        kb_ids=[KB_ID],
    )


def _edge(subject: Node, predicate: str, obj: Node, **kw) -> Edge:
    return Edge(
        subject_uid=subject.uid,
        predicate=predicate,
        object_uid=obj.uid,
        kb_id=KB_ID,
        doc_id=kw.get("doc_id"),
        version_id=kw.get("version_id"),
        chunk_ids=kw.get("chunk_ids", []),
        status=kw.get("status", "auto"),
        confidence=kw.get("confidence", 1.0),
    )


@pytest.fixture
async def store():
    s = Neo4jGraphStore(
        uri=NEO4J_URI,
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", ""),
        database=os.getenv("NEO4J_TEST_DATABASE", "neo4j"),
    )
    assert await s.ensure_schema(), "schema 初始化失败：检查 Neo4j 是否已启动、密码是否正确"
    await s.delete_kb(KB_ID)  # 清掉上次失败留下的数据
    try:
        yield s
    finally:
        await s.delete_kb(KB_ID)
        await s.close()


class TestLiveStore:
    async def test_health(self, store):
        assert await store.health() is True

    async def test_upsert_and_get(self, store):
        node = _node("Parameter", "sm_live", group="产流", unit="mm")
        await store.upsert([node], [])
        got = await store.get_node(node.uid)
        assert got is not None and got.name == "sm_live"
        assert got.props["group"] == "产流"

    async def test_idempotent_upsert_merges_lists(self, store):
        a = _node("Parameter", "ke_live", group="蒸散发")
        b = _node("Metric", "NSE_live")
        await store.upsert([a, b], [_edge(a, "AFFECTS", b, chunk_ids=[1], version_id=1)])
        await store.upsert([a, b], [_edge(a, "AFFECTS", b, chunk_ids=[2], version_id=1)])
        slice_ = await store.neighbors(a.uid)
        assert len(slice_.edges) == 1
        assert slice_.edges[0].chunk_ids == [1, 2]

    async def test_version_isolation(self, store):
        a = _node("Parameter", "wum_live", group="产流")
        b = _node("Metric", "KGE_live")
        await store.upsert([a, b], [_edge(a, "AFFECTS", b, version_id=1)])
        await store.upsert([], [_edge(a, "AFFECTS", b, version_id=2)])
        assert (await store.stats(kb_id=KB_ID))["edges"]["total"] == 2

    async def test_rejected_edge_hidden(self, store):
        a = _node("Parameter", "c_live", group="蒸散发")
        b = _node("Metric", "R_live")
        await store.upsert([a, b], [_edge(a, "AFFECTS", b, version_id=1)])
        await store.upsert([], [_edge(a, "AFFECTS", b, version_id=1, status="rejected")])
        slice_ = await store.neighbors(a.uid)
        assert slice_.edges == []

    async def test_two_hops(self, store):
        a = _node("Parameter", "wm_live", group="产流")
        m = _node("Parameter", "wlm_live", group="产流")
        b = _node("Metric", "NSE_live2")
        await store.upsert(
            [a, m, b],
            [_edge(a, "PART_OF", m, version_id=1), _edge(m, "AFFECTS", b, version_id=1)],
        )
        slice_ = await store.neighbors(a.uid, depth=2)
        assert {n.uid for n in slice_.nodes} >= {a.uid, m.uid, b.uid}

    async def test_search(self, store):
        node = _node("Parameter", "自由水蓄量live", group="水源划分")
        node.aliases.append("SM_LIVE_ALIAS")
        await store.upsert([node], [])
        assert await store.search("SM_LIVE_ALIAS", kb_id=KB_ID)
        assert await store.search("自由水蓄量live", kb_id=KB_ID)

    async def test_delete_kb_cleans_everything(self, store):
        a = _node("Parameter", "cleanup_live", group="汇流")
        b = _node("Metric", "cleanup_metric_live")
        await store.upsert([a, b], [_edge(a, "AFFECTS", b, version_id=1)])
        await store.delete_kb(KB_ID)
        stats = await store.stats(kb_id=KB_ID)
        assert stats["nodes"]["total"] == 0
        assert stats["edges"]["total"] == 0
