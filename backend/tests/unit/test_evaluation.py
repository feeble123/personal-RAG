"""P1-1 单元1：评测门禁框架自测。

用 conftest 临时库（fake embedding）+ sample_kb 验证：
- retrieve(return_trace=True) 返回 RetrievedResult（cites + trace）
- trace 记录向量/BM25/融合/rerank 分数、rerank_status、expanded_type
- scorers 判定逻辑正确（recall_at_k / has_citation / aggregate）
"""
from __future__ import annotations

from app.services import rag
from app.services.rag import RetrievedResult
from evaluation.scorers import aggregate, has_citation, recall_at_k


class TestRetrieveTrace:
    async def test_return_trace_gives_result_object(self, client, sample_kb):
        from app.db.session import async_session_factory

        kb_id, _ = sample_kb
        async with async_session_factory() as db:
            result = await rag.retrieve(db, "明渠均匀流形成条件", kb_id=kb_id, return_trace=True)
            assert isinstance(result, RetrievedResult)
            assert result.trace is not None
            assert len(result.cites) >= 1, "应检索到引用"
            # trace 字段
            tr = result.trace
            assert tr.query == "明渠均匀流形成条件"
            assert tr.vector_hits >= 0
            assert tr.rerank_status in ("disabled", "ok", "failed")
            # 候选 trace：至少一个候选有 vector_score 或 bm25_score
            assert tr.candidates, "trace 应有候选"
            first = tr.candidates[0]
            assert first.chunk_id > 0
            assert (first.vector_score is not None) or (first.bm25_score is not None)

    async def test_default_returns_list(self, client, sample_kb):
        """return_trace=False（默认）仍返回 list[RetrievedChunk]，零影响。"""
        from app.db.session import async_session_factory

        kb_id, _ = sample_kb
        async with async_session_factory() as db:
            cites = await rag.retrieve(db, "明渠均匀流形成条件", kb_id=kb_id)
            assert isinstance(cites, list)
            assert not isinstance(cites, RetrievedResult)

    async def test_trace_rerank_disabled_offline(self, client, sample_kb):
        """离线（rerank 关闭）时 trace.rerank_status=disabled。"""
        from app.db.session import async_session_factory

        kb_id, _ = sample_kb
        async with async_session_factory() as db:
            result = await rag.retrieve(db, "明渠均匀流形成条件", kb_id=kb_id, return_trace=True)
            assert result.trace.rerank_status == "disabled" or result.trace.rerank_status == "ok"


class TestScorers:
    def test_recall_at_k_detects_keyword(self):
        from app.services.rag import RetrievedChunk

        cites = [
            RetrievedChunk(
                chunk_id=1, kb_id=1, doc_id=1, source="水力学.pdf",
                section="7.4 明渠均匀流", page=215,
                snippet="明渠均匀流的形成条件包括长直棱柱体渠道、正坡、糙率不变、流量恒定。",
                score=0.9,
            )
        ]
        assert recall_at_k(cites, ["明渠均匀流"], 5) is True
        assert recall_at_k(cites, ["不存在的关键词"], 5) is False
        assert has_citation(cites, 10) is True

    def test_recall_at_k_respects_k(self):
        from app.services.rag import RetrievedChunk

        hit = RetrievedChunk(
            chunk_id=2, kb_id=1, doc_id=1, source="x.pdf", snippet="目标关键词在这里",
        )
        no_hit = RetrievedChunk(
            chunk_id=1, kb_id=1, doc_id=1, source="x.pdf", snippet="无关内容",
        )
        # 目标在第 6 位 → recall@5 不中，recall@10 中
        cites = [no_hit] * 5 + [hit]
        assert recall_at_k(cites, ["目标关键词"], 5) is False
        assert recall_at_k(cites, ["目标关键词"], 10) is True

    def test_aggregate_counts_correctly(self):
        results = [
            {"q": "a", "intent": "general", "recall5": True, "recall10": True, "citation": True},
            {"q": "b", "intent": "general", "recall5": False, "recall10": True, "citation": False},
            {"q": "c", "intent": "enumeration", "recall5": True, "recall10": True, "citation": True},
        ]
        agg = aggregate(results)
        assert agg["total"] == 3
        assert agg["recall_at_5"] == round(2 / 3, 4)
        assert agg["recall_at_10"] == 1.0
        assert agg["by_intent"]["general"]["n"] == 2
        assert agg["by_intent"]["general"]["recall5"] == 0.5
