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


class TestConditionOnNodes:
    """命中工况的节点排在前面并被标出来——编排层要靠它决定讲哪个分支的方案。"""

    async def test_命中20年一遇的节点排在前并打标(self, monkeypatch):
        class _Store:
            async def get_node(self, uid):
                return _Node(
                    uid,
                    {"cd:5": "20年一遇工况", "cd:1": "100年一遇工况"}[uid],
                    type_name="condition",
                    props={"frequency": 0.05 if uid == "cd:5" else 0.01},
                )

            async def neighbors(self, *a, **kw):
                return _Slice([], [])

        monkeypatch.setattr("app.services.graph.store.get_graph_store", lambda: _Store())

        async def fake_link(db, question, kb_id=None):
            return [
                retrieval.LinkedEntity(uid="cd:1", entity_type="condition", name="100年一遇工况",
                                       display="", matched="", score=1.0, source="graph"),
                retrieval.LinkedEntity(uid="cd:5", entity_type="condition", name="20年一遇工况",
                                       display="", matched="", score=1.0, source="graph"),
            ]

        monkeypatch.setattr(retrieval, "link_graph_entities", fake_link)
        facts, _, _, _ = await retrieval._graph_facts(None, "20年一遇怎么调度", kb_id=None)

        nodes = [f for f in facts if f["kind"] == "graph_node"]
        assert nodes[0]["name"] == "20年一遇工况"
        assert nodes[0]["condition_matched"] is True
        assert nodes[1]["condition_matched"] is False


class TestConditionPruning:
    """命中工况后只走那条分支——不把 100 年方案混进 20 年的答案里。"""

    async def test_只从命中工况的节点展开(self, monkeypatch):
        calls: list[str] = []

        class _Store:
            PROPS = {"cd:5": {"frequency": 0.05}, "cd:1": {"frequency": 0.01}}

            async def get_node(self, uid):
                return _Node(uid, f"{uid} 工况", type_name="condition", props=self.PROPS[uid])

            async def neighbors(self, uid, depth=1, rel_types=None, limit=200):
                calls.append(uid)
                target = f"{uid}:plan"
                return _Slice([_Node(target, f"{uid} 的方案")], [_Edge(uid, "STIPULATES", target)])

        monkeypatch.setattr("app.services.graph.store.get_graph_store", lambda: _Store())

        async def fake_link(db, question, kb_id=None):
            return [
                retrieval.LinkedEntity(uid="cd:1", entity_type="condition", name="100年一遇工况",
                                       display="", matched="", score=1.0, source="graph"),
                retrieval.LinkedEntity(uid="cd:5", entity_type="condition", name="20年一遇工况",
                                       display="", matched="", score=1.0, source="graph"),
            ]

        monkeypatch.setattr(retrieval, "link_graph_entities", fake_link)
        facts, _, _, _ = await retrieval._graph_facts(None, "20年一遇怎么调度", kb_id=None)

        # 命中分支会继续往下一跳走，这是对的；关键是**另一条分支一步都没走**
        assert "cd:5" in calls
        assert not any(uid.startswith("cd:1") for uid in calls), "不该展开 100 年一遇那条分支"
        edges = [f for f in facts if f["kind"] == "graph_edge"]
        assert all(e["subject_uid"].startswith("cd:5") for e in edges)


class TestForwardOnly:
    """命中工况后的展开必须顺着边的方向走。

    为什么：处置链里的「通知下游」「汇报」这类措施被 Ⅰ/Ⅱ/Ⅲ/Ⅳ 四级共用，
    双向展开会顺着它们爬到别的等级——问「20 年一遇」却答出「1000 年一遇」
    才有的动作，这比少答更糟。
    """

    async def test_只顺着边的方向走(self):
        store = _Store()
        nodes = {"A": _Node("A", "A")}
        edges = await retrieval._expand_graph(
            store, ["A"], nodes=nodes, depth=4,
            max_nodes=40, max_edges=120, rel_types=None, forward_only=True,
        )
        assert [e.predicate_name for _h, e in edges] == ["r1", "r2", "r3", "r4"]

    async def test_默认仍然双向(self):
        """没命中工况时保持双向展开——种子可能是链路末端（如某个乡镇）。"""

        class _Both:
            async def neighbors(self, uid, depth=1, rel_types=None, limit=200):
                # C 既在 B→C 的末端，也在 C→D 的起点
                return _Slice(
                    [_Node("B", "B"), _Node("D", "D")],
                    [_Edge("B", "r2", "C"), _Edge("C", "r3", "D")],
                )

        nodes = {"C": _Node("C", "C")}
        edges = await retrieval._expand_graph(
            _Both(), ["C"], nodes=nodes, depth=1,
            max_nodes=40, max_edges=120, rel_types=None,
        )
        predicates = {e.predicate_name for _h, e in edges}
        assert "r3" in predicates and "r2" in predicates
