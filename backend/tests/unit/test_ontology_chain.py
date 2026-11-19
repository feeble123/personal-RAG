"""链路骨架（`ontology/chain.py`）：顺序与证据由程序决定，措辞以后再说。

排序错了比措辞差严重得多——"下一步做什么"排到"用什么工具"后面，
读者就会照着错的顺序执行。
"""
from __future__ import annotations

from app.services.graph.ontology import chain as C


def _edge(subject, predicate, obj, *, hop=1, label="", evidence_text="", chunk_ids=None):
    return {
        "kind": "graph_edge",
        "subject": subject,
        "predicate": predicate,
        "predicate_label": label or predicate,
        "object": obj,
        "hop": hop,
        "evidence_text": evidence_text,
        "chunk_ids": chunk_ids or [],
    }


def _node(name, *, matched=False):
    return {
        "kind": "graph_node",
        "subject": f"uid:{name}",
        "name": name,
        "entity_type": "engineering_object",
        "entity_type_label": "工程对象",
        "condition_matched": matched,
    }


class TestPhase:
    def test_关系落到正确的档(self):
        assert C.phase_of("NEXT_STAGE") == "flow"
        assert C.phase_of("stipulates") == "basis"  # 历史小写边名也认
        assert C.phase_of("AFFECTS") == "impact"
        assert C.phase_of("MADE_UP_THING") == "other"


class TestBuildChain:
    def test_按人讲的顺序排_而不是发现顺序(self):
        facts = [
            _edge("下游村镇", "AFFECTS", "杨柳镇", hop=3),
            _edge("水库", "STIPULATES", "泄洪方案A", hop=1),
            _edge("数据准备", "NEXT_STAGE", "资料处理", hop=1),
            _edge("泄洪方案A", "PERFORMED_BY", "水务局", hop=2),
        ]
        built = C.build_chain(facts)
        phases = [s.phase for s in built.steps]
        assert phases == sorted(phases, key=C.PHASE_ORDER.index)
        assert built.steps[0].phase == "basis"

    def test_同一档里近的在前(self):
        facts = [
            _edge("a", "AFFECTS", "c", hop=3),
            _edge("a", "AFFECTS", "b", hop=1),
        ]
        built = C.build_chain(facts)
        assert [s.hop for s in built.steps] == [1, 3]

    def test_起点取命中工况的节点(self):
        facts = [_node("青溪水库"), _node("20年一遇工况", matched=True)]
        built = C.build_chain(facts)
        assert built.subject == "20年一遇工况"

    def test_没有命中工况时取第一个节点(self):
        built = C.build_chain([_node("青溪水库"), _node("别的")])
        assert built.subject == "青溪水库"

    def test_工况写进链路(self):
        built = C.build_chain([_node("x")], conditions=["20年一遇（P≈5.0%）"])
        assert built.conditions == ["20年一遇（P≈5.0%）"]
        assert any("20年一遇" in line for line in built.outline())

    def test_没证据的环节单独列出来(self):
        facts = [
            _edge("a", "STIPULATES", "b", evidence_text="规程第 8 页"),
            _edge("b", "AFFECTS", "c"),  # 没有任何证据
        ]
        built = C.build_chain(facts)
        assert len(built.gaps) == 1
        assert "b" in built.gaps[0]
        # 有证据的那条不进 gaps
        assert all("b" not in g for g in built.gaps[1:])

    def test_证据补全成文档与页码(self):
        def lookup(ids):
            return [
                {"chunk_id": i, "doc_name": "XX水库调度规程", "page": 171, "printed_page": 157}
                for i in ids
            ]

        built = C.build_chain([_edge("水库", "STIPULATES", "方案A", chunk_ids=[1, 2])], evidence_lookup=lookup)
        step = built.steps[0]
        assert len(step.evidence) == 2
        assert step.evidence[0]["doc_name"] == "XX水库调度规程"
        # 骨架句里带出处，且优先用书内印刷页码
        assert "p.157" in step.describe()

    def test_骨架文本分档输出(self):
        facts = [
            _edge("水库", "STIPULATES", "方案A", evidence_text="规程"),
            _edge("方案A", "PERFORMED_BY", "水务局"),
        ]
        lines = C.build_chain(facts).outline()
        assert lines[0] == "【起点】水库"
        assert "【工况与依据】" in lines
        assert "【谁处置】" in lines
