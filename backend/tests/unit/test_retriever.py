"""P0-11 检索服务契约：稳定字段 + 出处元数据补全。

契约一旦冻结，字段名/结构不再更改（未来 DSH 依赖此结构）：
- 输入 query/top_k/kb_id
- 输出 results[]: { text, score, source: { document_name, document_type, section,
  page, clause_no, formula_no, block_type, doc_id, chunk_id } }
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from app.db.models import Chunk, Document
from app.db.session import async_session_factory
from app.services import retriever
from app.services.retriever import RetrievalResult, RetrievalSource, results_to_dict

# 契约字段（冻结）：新增/改名字段必须同步这里，否则测试失败。
_CONTRACT_KEYS = {
    "document_name",
    "document_type",
    "section",
    "page",
    "clause_no",
    "formula_no",
    "block_type",
    "doc_id",
    "chunk_id",
}


class TestContractStructure:
    def test_source_has_frozen_fields(self):
        """source 字段集必须与契约一致（防未来无意改字段名破坏 DSH）。"""
        s = RetrievalSource(document_name="测试.pdf")
        assert set(s.to_dict().keys()) == _CONTRACT_KEYS

    def test_result_structure(self):
        """results[] 每项含 text/score/source。"""
        r = RetrievalResult(
            text="内容", score=0.9,
            source=RetrievalSource(document_name="x.pdf", document_type="textbook"),
        )
        d = r.to_dict()
        assert set(d.keys()) == {"text", "score", "source"}
        assert d["score"] == 0.9
        assert d["source"]["document_type"] == "textbook"

    def test_results_to_dict_shape(self):
        """完整输出体：{"results": [...]}。"""
        r = RetrievalResult(
            text="内容", score=0.5,
            source=RetrievalSource(document_name="x.pdf"),
        )
        body = results_to_dict([r])
        assert list(body.keys()) == ["results"]
        assert len(body["results"]) == 1


class TestRetrieverEndToEnd:
    async def test_retriever_returns_contract(self, client, admin_headers, sample_kb):
        """真实库检索：返回契约结构 + 出处元数据（doc_type/section/page）。"""
        kb_id, doc_id = sample_kb
        results = await retriever.retrieve("明渠均匀流", top_k=3, kb_id=kb_id)
        assert results, "应返回至少一条结果"
        for r in results:
            assert isinstance(r, RetrievalResult)
            assert r.text
            assert isinstance(r.score, float)
            src = r.source
            assert src.document_name
            assert src.document_type in ("textbook", "standard", "manual", "other")
            # sample_kb 的 md 无条款号/公式号，应为 None
            assert isinstance(src.clause_no, (str, type(None)))
            assert isinstance(src.formula_no, (str, type(None)))
            assert src.block_type in ("text", "table")
            assert src.doc_id is not None
            assert src.chunk_id is not None

    async def test_retriever_top_k(self, client, admin_headers, sample_kb):
        """top_k 控制返回条数。"""
        kb_id, _ = sample_kb
        results = await retriever.retrieve("明渠均匀流", top_k=2, kb_id=kb_id)
        assert len(results) <= 2

    async def test_retriever_metadata_from_db(self, client, admin_headers, sample_kb):
        """检索结果的出处与 DB 落库一致（doc_type/section 来自真实文档）。"""
        kb_id, doc_id = sample_kb
        async with async_session_factory() as db:
            doc = await db.get(Document, doc_id)
            # sample_kb 未选类型，默认 other
            assert doc.doc_type == "other"
        results = await retriever.retrieve("明渠均匀流", top_k=3, kb_id=kb_id)
        for r in results:
            if r.source.doc_id == doc_id:
                assert r.source.document_type == "other"
                assert r.source.document_name  # 有来源文件名


class TestContractFrozen:
    def test_retrieval_source_field_count_stable(self):
        """契约 source 字段数量稳定（防止新增字段破坏已对接方）。"""
        s = RetrievalSource(document_name="x.pdf")
        assert len(s.to_dict()) == 9, f"契约字段数变化: {list(s.to_dict().keys())}"
