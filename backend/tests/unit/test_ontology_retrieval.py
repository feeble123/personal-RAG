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

    async def test_to_dict_shape(self, stub):
        pack = await retrieval.retrieve_with_ontology(None, "sm", min_facts=0)
        data = pack.to_dict()
        assert set(data) >= {
            "question", "facts", "entities", "evidence",
            "fallback_used", "sources", "latency_ms", "notes",
        }
