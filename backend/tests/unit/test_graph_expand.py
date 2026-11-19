"""图上多跳展开（`retrieval._expand_graph` / `_graph_limits`）。

为什么要单测：链路问答（水库→频率→调度方案→闸门→下游村镇→响应）就是靠这里
走出去的，而它同时要满足三个互相拉扯的要求——跳够、别爆、顺序对。
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.core.config import settings
from app.services.graph.ontology import retrieval


@dataclass
class _Node:
    uid: str
    name: str
    type_name: str = "engineering_object"
    props: dict | None = None


@dataclass
class _Edge:
    subject_uid: str
    predicate_name: str
    object_uid: str
    evidence_text: str = ""
    chunk_ids: list | None = None


class _Slice:
    def __init__(self, nodes, edges):
        self.nodes = nodes
        self.edges = edges


class _Store:
    """一条链：A -r1-> B -r2-> C -r3-> D -r4-> E"""

    CHAIN = {
        "A": ("r1", "B"),
        "B": ("r2", "C"),
        "C": ("r3", "D"),
        "D": ("r4", "E"),
    }

    def __init__(self):
        self.calls: list[tuple[str, list | None]] = []

    async def neighbors(self, uid, depth=1, rel_types=None, limit=200):
        self.calls.append((uid, rel_types))
        link = self.CHAIN.get(uid)
        if link is None:
            return _Slice([], [])
        predicate, target = link
        if rel_types and predicate not in rel_types:
            return _Slice([_Node(target, target)], [])
        return _Slice([_Node(target, target)], [_Edge(uid, predicate, target, f"{uid}->{target} 的出处")])


async def _expand(store, seeds, depth=3, max_nodes=40, max_edges=120, rel_types=None):
    nodes = {uid: _Node(uid, uid) for uid in seeds}
    edges = await retrieval._expand_graph(
        store,
        list(seeds),
        nodes=nodes,
        depth=depth,
        max_nodes=max_nodes,
        max_edges=max_edges,
        rel_types=rel_types,
    )
    return nodes, edges


class TestExpandGraph:
    async def test_三跳以内的边都拿到(self):
        store = _Store()
        _nodes, edges = await _expand(store, ["A"], depth=3)
        assert [e.predicate_name for _hop, e in edges] == ["r1", "r2", "r3"]
        assert [hop for hop, _e in edges] == [1, 2, 3]

    async def test_默认三跳_第四跳不展开(self):
        store = _Store()
        _nodes, edges = await _expand(store, ["A"])
        assert "r4" not in {e.predicate_name for _h, e in edges}

    async def test_边数上限一到就停(self):
        store = _Store()
        _nodes, edges = await _expand(store, ["A"], depth=4, max_edges=2)
        assert len(edges) == 2

    async def test_节点上限一到就不再往外走(self):
        store = _Store()
        _nodes, edges = await _expand(store, ["A"], depth=4, max_nodes=2)
        # 种子 A 占一个名额，只允许再进一个节点 → 只能拿到 r1
        assert [e.predicate_name for _h, e in edges] == ["r1"]

    async def test_原地打转不会死循环(self):
        class LoopStore:
            async def neighbors(self, uid, depth=1, rel_types=None, limit=200):
                # 每次都说"我在 B"，且 B 指回 A
                return _Slice([_Node("B", "B")], [_Edge(uid, "r", "A" if uid == "B" else "B")])

        store = LoopStore()
        _nodes, edges = await _expand(store, ["A"], depth=4)
        assert len(edges) == 2  # A→B 与 B→A 各一次，之后被去重挡住

    async def test_单点失败不拖垮整条链路(self):
        class FlakyStore(_Store):
            async def neighbors(self, uid, depth=1, rel_types=None, limit=200):
                if uid == "B":
                    raise RuntimeError("图库抖了一下")
                return await super().neighbors(uid, depth=depth, rel_types=rel_types, limit=limit)

        _nodes, edges = await _expand(FlakyStore(), ["A"], depth=4)
        assert [e.predicate_name for _h, e in edges] == ["r1"]  # 第二跳断了，但不抛异常

    async def test_关系白名单会传给存储层(self):
        store = _Store()
        await _expand(store, ["A"], depth=1, rel_types=["r1"])
        assert store.calls[0] == ("A", ["r1"])


class TestGraphLimits:
    def test_深度被硬上限钳住(self, monkeypatch):
        monkeypatch.setattr(settings, "ontology_graph_depth", 99)
        monkeypatch.setattr(settings, "ontology_graph_max_depth", 4)
        depth, *_ = retrieval._graph_limits()
        assert depth == 4

    def test_关系白名单解析并去空(self, monkeypatch):
        monkeypatch.setattr(settings, "ontology_graph_relations", " AFFECTS , ,USED ")
        *_rest, rel_types = retrieval._graph_limits()
        assert rel_types == ["AFFECTS", "USED"]

    def test_留空表示不过滤(self, monkeypatch):
        monkeypatch.setattr(settings, "ontology_graph_relations", "  ")
        *_rest, rel_types = retrieval._graph_limits()
        assert rel_types is None

    def test_默认值是一个能走通链路的深度(self):
        assert 2 <= settings.ontology_graph_depth <= 4
        assert settings.ontology_graph_max_edges >= settings.ontology_graph_max_nodes


@pytest.mark.parametrize("bad_depth", [0, -3])
def test_深度非法时至少走一跳(monkeypatch, bad_depth):
    monkeypatch.setattr(settings, "ontology_graph_depth", bad_depth)
    depth, *_ = retrieval._graph_limits()
    assert depth == 1
