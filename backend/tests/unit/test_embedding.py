"""Embedding 单元测试：FakeEmbedding 确定性（离线，无需 API Key）。"""
from __future__ import annotations

from app.services.embedding import FakeEmbedding


class TestFakeEmbedding:
    def setup_method(self):
        self.emb = FakeEmbedding(dim=64)

    def test_same_text_same_vector(self):
        v1 = self.emb.embed_query("明渠均匀流")
        v2 = self.emb.embed_query("明渠均匀流")
        assert v1 == v2
        assert len(v1) == 64

    def test_different_text_different_vector(self):
        assert self.emb.embed_query("水力学") != self.emb.embed_query("工程水文学")

    def test_document_batch(self):
        vectors = self.emb.embed_documents(["文本一", "文本二", "文本三"])
        assert len(vectors) == 3
        assert all(len(v) == 64 for v in vectors)
        assert vectors[0] != vectors[1]

    def test_vector_values_in_range(self):
        v = self.emb.embed_query("测试")
        assert all(0.0 <= x <= 1.0 for x in v)
