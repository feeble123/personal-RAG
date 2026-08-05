"""语义缓存单元测试：余弦相似度计算 + 检索作用域（kb/doc）隔离。"""
from __future__ import annotations

import pytest

from app.db.session import async_session_factory, init_db
from app.services import semantic_cache
from app.services.semantic_cache import _cosine


class TestCosine:
    def test_identical_vectors(self):
        assert _cosine([1, 0, 0], [1, 0, 0]) == pytest.approx(1.0)

    def test_orthogonal_vectors(self):
        assert _cosine([1, 0], [0, 1]) == pytest.approx(0.0)

    def test_opposite_vectors(self):
        assert _cosine([1, 0], [-1, 0]) == pytest.approx(-1.0)

    def test_scale_invariant(self):
        a = [1, 2, 3]
        b = [2, 4, 6]  # 同方向不同长度
        assert _cosine(a, b) == pytest.approx(1.0)

    def test_zero_vector(self):
        assert _cosine([0, 0], [1, 1]) == 0.0
        assert _cosine([0, 0], [0, 0]) == 0.0

    def test_similar_vectors_high_score(self):
        # 两路相似向量（浮点）→ 高分
        a = [0.1, 0.9, 0.2]
        b = [0.12, 0.88, 0.21]
        assert _cosine(a, b) > 0.99


class TestCacheScopeIsolation:
    """BUG-B：缓存命中必须检索作用域（kb_id/doc_scope）完全一致，否则重放旧库答案。"""

    VEC = [1.0, 0.0, 0.0]

    async def _store(self, answer, kb_id, doc_scope):
        async with async_session_factory() as db:
            await semantic_cache.store(db, self.VEC, "保障体系", answer, [], kb_id=kb_id, doc_scope=doc_scope)

    async def _find(self, kb_id, doc_scope):
        async with async_session_factory() as db:
            hit = await semantic_cache.find(db, self.VEC, "保障体系", kb_id=kb_id, doc_scope=doc_scope)
            return hit[0] if hit else None

    @pytest.fixture(autouse=True)
    async def _clean(self):
        await init_db()  # 测试库建表（含语义缓存迁移列）
        await semantic_cache.clear_cache()
        yield
        await semantic_cache.clear_cache()

    async def test_same_kb_hits(self):
        await self._store("答案A", kb_id=1, doc_scope=None)
        assert await self._find(1, None) == "答案A"

    async def test_switched_kb_misses(self):
        """切库后同一问题不得重放旧库答案。"""
        await self._store("答案A", kb_id=1, doc_scope=None)
        assert await self._find(2, None) is None
        assert await self._find(None, None) is None  # 切回跨库也不命中

    async def test_doc_scope_misses(self):
        """点名不同文档（doc_scope 不同）不得互相重放。"""
        await self._store("答案A", kb_id=None, doc_scope="4")
        assert await self._find(None, "4") == "答案A"
        assert await self._find(None, "5") is None
        assert await self._find(None, None) is None
