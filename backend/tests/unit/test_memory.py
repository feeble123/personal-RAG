"""问答记忆库单元测试：余弦 + 按用户/作用域隔离 + 严格阈值 + 去重 + 状态纠偏 + 容量 + bad 优先。"""
from __future__ import annotations

import math

import pytest
from sqlalchemy import delete, func, select

from app.db.models import QaMemory
from app.db.session import async_session_factory, init_db
from app.services import memory
from app.services.memory import MemoryConfig, _cosine

CFG = MemoryConfig(enabled=True, threshold=0.93, max_entries=300, pool=100)


class TestCosine:
    def test_identical(self):
        assert _cosine([1, 0, 0], [1, 0, 0]) == pytest.approx(1.0)

    def test_orthogonal(self):
        assert _cosine([1, 0], [0, 1]) == pytest.approx(0.0)

    def test_opposite(self):
        assert _cosine([1, 0], [-1, 0]) == pytest.approx(-1.0)

    def test_scale_invariant(self):
        assert _cosine([1, 2, 3], [2, 4, 6]) == pytest.approx(1.0)

    def test_zero_vector(self):
        assert _cosine([0, 0], [1, 1]) == 0.0

    def test_known_score(self):
        # [1,0] 与 [0.94, sqrt(1-0.94²)] 余弦恰为 0.94
        b = [0.94, math.sqrt(1 - 0.94**2)]
        assert _cosine([1, 0], b) == pytest.approx(0.94, abs=1e-6)


class TestMemoryRecall:
    VEC = [1.0, 0.0, 0.0]

    async def _remember(self, user_id=1, kb_id=1, doc_scope=None, style=None, answer="答案A",
                        status="good", vec=None, subject="保障体系"):
        async with async_session_factory() as db:
            await memory.remember(
                db, vec or self.VEC, subject, "保障体系有什么要求？", answer, [],
                user_id=user_id, kb_id=kb_id, doc_scope=doc_scope, style=style,
                status=status, config=CFG,
            )

    async def _recall(self, user_id=1, kb_id=1, doc_scope=None, style=None, vec=None, subject="保障体系"):
        async with async_session_factory() as db:
            return await memory.recall(
                db, vec or self.VEC, subject,
                user_id=user_id, kb_id=kb_id, doc_scope=doc_scope, style=style, config=CFG,
            )

    @pytest.fixture(autouse=True)
    async def _clean(self):
        await init_db()
        async with async_session_factory() as db:
            await db.execute(delete(QaMemory))
            await db.commit()
        yield
        async with async_session_factory() as db:
            await db.execute(delete(QaMemory))
            await db.commit()

    async def test_same_user_same_kb_hits(self):
        await self._remember()
        res = await self._recall()
        assert res is not None and res.status == "good" and res.answer == "答案A"
        assert res.force_rerank is False

    async def test_different_user_misses(self):
        """按用户隔离：用户2 不得命中用户1 的记忆。"""
        await self._remember(user_id=1)
        assert await self._recall(user_id=2) is None

    async def test_switched_kb_misses(self):
        await self._remember(kb_id=1)
        assert await self._recall(kb_id=2) is None
        assert await self._recall(kb_id=None) is None

    async def test_doc_scope_misses(self):
        await self._remember(doc_scope="4")
        assert await self._recall(doc_scope="4") is not None
        assert await self._recall(doc_scope="5") is None
        assert await self._recall(doc_scope=None) is None

    async def test_style_misses(self):
        await self._remember(style="standard")
        assert await self._recall(style="standard") is not None
        assert await self._recall(style="tutorial") is None

    async def test_strict_threshold_hit(self):
        """sim≈0.94 ≥ 0.93 → 命中。"""
        hit_vec = [0.94, math.sqrt(1 - 0.94**2), 0.0]
        await self._remember(vec=hit_vec)
        res = await self._recall()
        assert res is not None and res.score >= 0.93

    async def test_strict_threshold_miss(self):
        """sim≈0.92 < 0.93 → 不命中（严格复用）。"""
        miss_vec = [0.92, math.sqrt(1 - 0.92**2), 0.0]
        await self._remember(vec=miss_vec)
        assert await self._recall() is None

    async def test_bad_hit_force_rerank(self):
        await self._remember(status="bad", answer="错答案")
        res = await self._recall()
        assert res is not None and res.status == "bad" and res.force_rerank is True

    async def test_bad_priority_over_good(self):
        """同作用域同时有 good(0.94) 与 bad(0.95) → 负面优先。"""
        bad_vec = [0.95, math.sqrt(1 - 0.95**2), 0.0]
        good_vec = [0.94, math.sqrt(1 - 0.94**2), 0.0]
        await self._remember(vec=good_vec, status="good")
        await self._remember(vec=bad_vec, status="bad")
        res = await self._recall()
        assert res is not None and res.status == "bad" and res.force_rerank is True


class TestMemoryRemember:
    VEC = [1.0, 0.0, 0.0]

    async def _count(self, user_id=1, kb_id=1) -> int:
        async with async_session_factory() as db:
            return (
                await db.scalar(
                    select(func.count()).select_from(QaMemory).where(
                        QaMemory.user_id == user_id, QaMemory.kb_id == kb_id
                    )
                )
            ) or 0

    @pytest.fixture(autouse=True)
    async def _clean(self):
        await init_db()
        async with async_session_factory() as db:
            await db.execute(delete(QaMemory))
            await db.commit()
        yield
        async with async_session_factory() as db:
            await db.execute(delete(QaMemory))
            await db.commit()

    async def test_dedup_similar_question_updates(self):
        """相似问题重复沉淀 → 仅 1 行且 answer 为最新。"""
        async with async_session_factory() as db:
            await memory.remember(db, self.VEC, "保障体系", "保障体系有什么要求？", "答案V1", [],
                                  user_id=1, kb_id=1, config=CFG)
        async with async_session_factory() as db:
            await memory.remember(db, self.VEC, "保障体系", "保障体系有什么要求？", "答案V2", [],
                                  user_id=1, kb_id=1, config=CFG)
        assert await self._count() == 1
        async with async_session_factory() as db:
            row = (await db.execute(
                select(QaMemory).where(QaMemory.user_id == 1, QaMemory.kb_id == 1)
            )).scalars().first()
            assert row.answer == "答案V2"

    async def test_state_correction_good_to_bad(self):
        """👍 后相似问题再 👎 → 状态纠偏为 bad，仅 1 行。"""
        async with async_session_factory() as db:
            await memory.remember(db, self.VEC, "保障体系", "问题", "好答案", [],
                                  user_id=1, kb_id=1, status="good", config=CFG)
        async with async_session_factory() as db:
            await memory.remember(db, self.VEC, "保障体系", "问题", "被踩答案", [],
                                  user_id=1, kb_id=1, status="bad", config=CFG)
        assert await self._count() == 1
        async with async_session_factory() as db:
            row = (await db.execute(
                select(QaMemory).where(QaMemory.user_id == 1, QaMemory.kb_id == 1)
            )).scalars().first()
            assert row.status == "bad"

    async def test_capacity_eviction(self):
        """max_entries=2 存 3 条不同 → 最旧被淘汰，行数 ≤ 2。"""
        cfg = MemoryConfig(enabled=True, threshold=0.93, max_entries=2, pool=100)
        for i, v in enumerate([[1, 0, 0], [0, 1, 0], [0, 0, 1]]):
            async with async_session_factory() as db:
                await memory.remember(db, v, f"主题{i}", f"问题{i}", f"答案{i}", [],
                                      user_id=1, kb_id=1, config=cfg)
        assert await self._count() <= 2

    async def test_record_feedback_up_down(self):
        async with async_session_factory() as db:
            ok = await memory.record_feedback(
                db, user_id=1, question="问题", answer="答案", citations=[],
                feedback="up", query_vector=self.VEC, subject="保障体系",
                kb_id=1, config=CFG,
            )
            assert ok is True
        assert await self._count() == 1
        async with async_session_factory() as db:
            row = (await db.execute(
                select(QaMemory).where(QaMemory.user_id == 1)
            )).scalars().first()
            assert row.status == "good"
