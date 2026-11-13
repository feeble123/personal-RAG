"""检索编排：本体够用就不回退；不够才回退 RAG；某一环失败只降级不抛。"""
from __future__ import annotations

import pytest

import app.services.graph.ontology.retrieval as retrieval


def _param_fact(name: str = "sm") -> dict:
    return {
        "kind": "parameter_prior",
        "subject": name,
        "range": [5, 50],
        "evidence": [{"note": "手册", "traceable": True}],
        "source": "ontology",
    }


def _graph_fact() -> dict:
    return {"kind": "graph_edge", "subject": "青溪水库", "predicate": "downstream_of", "object": "河段1"}


def _rag_fact() -> dict:
    return {"kind": "text_chunk", "chunk_id": 1, "snippet": "原文片段", "source": "rag"}


class _Entities:
    def to_dict(self):
        return {"uid": "pn:sm", "name": "sm"}


@pytest.fixture
def stub(monkeypatch):
    """默认桩：本体不给事实、图不给事实、RAG 给 2 条。"""
    state = {"params": [], "graph": [], "rag": [_rag_fact(), _rag_fact()]}

    async def fake_params(db, question, **kw):
        return state["params"], []

    async def fake_graph(db, question, **kw):
        return state["graph"], [], []

    async def fake_rag(db, question, kb_id):
        return state["rag"]

    monkeypatch.setattr(retrieval, "_parameter_facts", fake_params)
    monkeypatch.setattr(retrieval, "_graph_facts", fake_graph)
    monkeypatch.setattr(retrieval, "_rag_facts", fake_rag)
    return state


class TestFallback:
    async def test_ontology_enough_no_fallback(self, stub):
        stub["params"] = [_param_fact("sm"), _param_fact("ke"), _param_fact("cg")]
        pack = await retrieval.retrieve_with_ontology(None, "sm 怎么取", min_facts=3)
        assert pack.fallback_used is False
        assert "ontology" in pack.sources
        assert "rag" not in pack.sources
        assert len(pack.facts) == 3

    async def test_few_facts_falls_back(self, stub):
        stub["params"] = [_param_fact("sm")]
        pack = await retrieval.retrieve_with_ontology(None, "sm 怎么取", min_facts=3)
        assert pack.fallback_used is True
        assert "rag" in pack.sources
        assert any(f["kind"] == "text_chunk" for f in pack.facts)
        assert any("回退" in n for n in pack.notes)

    async def test_single_card_is_enough_by_default(self, stub):
        """一个参数问题的正确形态就是一张参数卡。

        默认门槛必须让「1 条参数事实」算够用——否则每个参数问题都会被判成
        "本体不够"而回退向量检索（实测 2ms 变 22s，还会掺进无关表格）。
        """
        stub["params"] = [_param_fact("sm")]
        pack = await retrieval.retrieve_with_ontology(None, "sm 这个参数一般取多少")
        assert pack.fallback_used is False
        assert pack.sources == ["ontology"]
        assert len(pack.facts) == 1

    async def test_zero_facts_falls_back_by_default(self, stub):
        """本体一条事实都拿不到 = 本体检索失败，这时才回退。"""
        pack = await retrieval.retrieve_with_ontology(None, "混凝土浇筑温度要求")
        assert pack.fallback_used is True
        assert "rag" in pack.sources
        assert any("本体只给出 0 条事实" in n for n in pack.notes)

    async def test_fallback_note_counts_pre_fallback_facts(self, stub):
        """提示语里的"本体只给出 N 条"必须是回退前的事实数，不能把 RAG 片段也算进去。"""
        stub["params"] = [_param_fact("sm")]
        pack = await retrieval.retrieve_with_ontology(None, "sm 怎么取", min_facts=3)
        assert any("本体只给出 1 条事实" in n for n in pack.notes)

    async def test_fallback_can_be_disabled(self, stub):
        pack = await retrieval.retrieve_with_ontology(
            None, "sm 怎么取", min_facts=3, allow_fallback=False
        )
        assert pack.fallback_used is False
        assert pack.facts == []

    async def test_evidence_aggregated(self, stub):
        stub["params"] = [_param_fact("sm")]
        pack = await retrieval.retrieve_with_ontology(None, "sm", min_facts=1)
        assert pack.evidence and pack.evidence[0]["traceable"] is True


class TestChineseLabels:
    """界面上一律中文：关系和实体类型都要带中文名，不能把 downstream_of 露给人看。"""

    async def test_graph_facts_carry_chinese_labels(self, monkeypatch):
        class _Node:
            def __init__(self, uid, name, type_name):
                self.uid, self.name, self.type_name, self.props = uid, name, type_name, {}

        class _Edge:
            subject_uid, predicate_name, object_uid = "eo:1", "downstream_of", "rv:2"
            evidence_text, chunk_ids = "", []

        class _Slice:
            nodes = [_Node("eo:1", "青溪水库", "engineering_object"), _Node("rv:2", "下游河段2", "river")]
            edges = [_Edge()]

        class _Store:
            async def get_node(self, uid):
                return _Node("eo:1", "青溪水库", "engineering_object")

            async def neighbors(self, *a, **kw):
                return _Slice()

        monkeypatch.setattr(
            "app.services.graph.store.get_graph_store", lambda: _Store()
        )

        async def fake_link(db, question, kb_id=None):
            return [
                retrieval.LinkedEntity(
                    uid="eo:1", entity_type="engineering_object", name="青溪水库",
                    display="青溪水库", matched="青溪水库", score=1.0, source="graph",
                )
            ]

        monkeypatch.setattr(retrieval, "link_graph_entities", fake_link)
        facts, _, _ = await retrieval._graph_facts(None, "青溪水库下游", kb_id=None)

        node = next(f for f in facts if f["kind"] == "graph_node")
        edge = next(f for f in facts if f["kind"] == "graph_edge")
        assert node["entity_type_label"] == "工程对象"
        assert edge["predicate_label"] == "下游"  # 内置防洪链关系也要中文

    def test_relation_labels_cover_builtin_flood_chain(self):
        """`/graph` 的 relation_labels 只取 TBox 会让存量内置关系露英文。"""
        from app.modules.ontology.routes import _relation_labels

        class _Rel:
            display_name = "属于"
            description = None

        class _Reg:
            relation_types = {"PART_OF": _Rel()}

        labels = _relation_labels(_Reg())
        assert labels["PART_OF"] == "属于"
        assert labels["downstream_of"] == "下游"
        assert labels["located_in"] == "位于"
        assert labels["responsible"] == "责任"


class TestDegradation:
    async def test_parameter_failure_degrades(self, monkeypatch, stub):
        async def boom(*a, **kw):
            raise RuntimeError("本体炸了")

        monkeypatch.setattr(retrieval, "_parameter_facts", boom)
        pack = await retrieval.retrieve_with_ontology(None, "sm", min_facts=1)
        assert any("本体查询失败" in n for n in pack.notes)
        assert pack.fallback_used is True  # 本体挂了 → 回退照常

    async def test_graph_failure_degrades(self, monkeypatch, stub):
        async def boom(*a, **kw):
            raise RuntimeError("图库炸了")

        monkeypatch.setattr(retrieval, "_graph_facts", boom)
        pack = await retrieval.retrieve_with_ontology(None, "青溪水库", min_facts=1)
        assert any("图库查询失败" in n for n in pack.notes)

    async def test_rag_failure_does_not_raise(self, monkeypatch, stub):
        async def boom(*a, **kw):
            raise RuntimeError("检索炸了")

        monkeypatch.setattr(retrieval, "_rag_facts", boom)
        pack = await retrieval.retrieve_with_ontology(None, "x", min_facts=1)
        assert any("回退检索失败" in n for n in pack.notes)

    async def test_rag_facts_read_snippet_field(self, monkeypatch):
        """RetrievedChunk 的正文在 snippet 上；取错字段会得到空事实。"""
        import app.services.rag as rag_mod

        class _Item:
            doc_id, chunk_id, page, section = 7, 9, 3, "5.2"
            snippet, score = "原文片段" * 100, 0.5  # 400 字，验证截断到 300

        async def fake_retrieve(*a, **kw):
            return [_Item()]

        monkeypatch.setattr(rag_mod, "retrieve", fake_retrieve)
        facts = await retrieval._rag_facts(None, "q", None)
        assert facts[0]["chunk_id"] == 9
        assert facts[0]["snippet"].startswith("原文片段")
        assert len(facts[0]["snippet"]) == 300  # 截断到 300 字

    async def test_to_dict_shape(self, stub):
        pack = await retrieval.retrieve_with_ontology(None, "sm", min_facts=0)
        data = pack.to_dict()
        assert set(data) >= {
            "question", "facts", "entities", "evidence",
            "fallback_used", "sources", "latency_ms", "notes",
        }
