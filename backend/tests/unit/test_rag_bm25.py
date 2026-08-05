"""RAG 检索 / BM25 单元测试：余弦相似度 / jieba 分词 / 索引检索排序。"""
from __future__ import annotations

import pytest

from app.services import bm25
from app.services.rag import _cosine


class TestRagCosine:
    """rag._cosine（向量排序核心，v2 检索用）。"""

    def test_identical(self):
        assert _cosine([1, 0, 0], [1, 0, 0]) == pytest.approx(1.0)

    def test_orthogonal(self):
        assert _cosine([1, 0], [0, 1]) == pytest.approx(0.0)

    def test_opposite(self):
        assert _cosine([1, 0], [-1, 0]) == pytest.approx(-1.0)

    def test_zero_vector(self):
        assert _cosine([0, 0], [1, 1]) == 0.0

    def test_distinguishability(self):
        # 相关 vs 无关应区分明显（检索质量的关键）
        related = _cosine([0.1, 0.9, 0.2], [0.11, 0.88, 0.22])
        unrelated = _cosine([0.1, 0.9, 0.2], [0.9, 0.1, 0.7])
        assert related > 0.95
        assert unrelated < 0.5
        assert related - unrelated > 0.4


class TestBM25:
    def test_tokenize_chinese(self):
        tokens = bm25.tokenize("明渠均匀流的形成条件")
        assert len(tokens) > 0
        assert all(isinstance(t, str) and t for t in tokens)

    def test_rebuild_and_search_ranking(self):
        kb_id = 999
        bm25.rebuild(
            kb_id,
            [
                (1, "明渠均匀流 水流 恒定 渠道 水深"),
                (2, "设计洪水 暴雨 汇流 产流"),
                (3, "水工建筑物 大坝 泄水 闸门"),
            ],
        )
        try:
            hits = bm25.search(kb_id, "明渠 水深", k=2)
            assert hits, "应有检索结果"
            top_id = hits[0][0]
            assert top_id == 1  # 文档 1 最相关
            # 分数降序
            scores = [s for _, s in hits]
            assert scores == sorted(scores, reverse=True)
            assert bm25.has_kb(kb_id) is True
            assert kb_id in bm25.all_kb_ids()
        finally:
            bm25.remove_kb(kb_id)

    def test_search_unknown_kb_returns_empty(self):
        assert bm25.search(123456, "任意查询") == []
        assert bm25.has_kb(123456) is False


class TestDocTitleResolution:
    """问题中点名文档（《书名》/「XXX中」）的候选提取与匹配（BUG-A）。"""

    from types import SimpleNamespace

    DOCS = [
        SimpleNamespace(id=6, filename="重庆市防汛抗旱应急预案.pdf"),
        SimpleNamespace(id=4, filename="数字孪生农村供水工程建设技术指南（试行）.pdf"),
        SimpleNamespace(id=3, filename="数字孪生水利工程建设技术导则（试行）.pdf"),
        SimpleNamespace(id=5, filename="水利技术标准编写规定.pdf"),
    ]

    def test_candidates_from_book_title(self):
        from app.services.rag import _doc_name_candidates

        q = "在《数字孪生农村供水工程建设技术指南》规范中，保障体系有什么要求？"
        assert "数字孪生农村供水工程建设技术指南" in _doc_name_candidates(q)

    def test_candidates_from_name_zhong(self):
        from app.services.rag import _doc_name_candidates

        q = "重庆市防汛抗旱应急预案中后期处置包括哪些内容？请详细说明"
        cands = _doc_name_candidates(q)
        assert "重庆市防汛抗旱应急预案" in cands

    def test_match_exact(self):
        from app.services.rag import _match_document

        doc = _match_document(self.DOCS, "重庆市防汛抗旱应急预案")
        assert doc is not None and doc.id == 6

    def test_match_substring_with_suffix(self):
        from app.services.rag import _match_document

        doc = _match_document(self.DOCS, "数字孪生农村供水工程建设技术指南")
        assert doc is not None and doc.id == 4

    def test_match_generic_word_rejected(self):
        from app.services.rag import _match_document

        # 泛词「预案」不应命中整份预案名（长短比 < 0.4）
        assert _match_document(self.DOCS, "预案") is None

    def test_no_match_returns_none(self):
        from app.services.rag import _match_document

        assert _match_document(self.DOCS, "数字孪生工程") is None
