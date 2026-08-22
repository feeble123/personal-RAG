"""P0-8 影子索引：build → 核对 → 原子切换；失败旧 collection 原样可查。

直接测 vector_store 的影子索引原语（真实 Chroma，临时目录隔离）：
- build_shadow → swap_shadow_to_active 后，新 chunks 可查、旧 chunks 不可查
- 影子写入失败（注入）→ active collection 原样保留、旧 chunks 仍可查（核心：故障可用率 100%）
- count 核对由 manager 层做；swap 改名后全局查询生效
"""
from __future__ import annotations

import asyncio

import pytest

from app.services import vector_store

pytestmark = pytest.mark.asyncio

# 测试用 fake embedding：固定 8 维向量（conftest 的 FAKE_EMBEDDING 维度）
_DIM = 8


def _vec(seed: int) -> list[float]:
    """确定性伪向量（归一化，保证 cosine 有意义）。"""
    import math

    raw = [float((seed * 31 + i * 7) % 17) / 17.0 for i in range(_DIM)]
    norm = math.sqrt(sum(v * v for v in raw)) or 1.0
    return [v / norm for v in raw]


def _chunk_ids(n: int, offset: int = 0) -> list[str]:
    return [str(offset + i) for i in range(n)]


async def _reset_active():
    """清空 active + shadow，确保每测试独立起点。"""
    await asyncio.to_thread(vector_store.reset_collection)
    await asyncio.to_thread(vector_store.drop_shadow)


class TestShadowSwap:
    async def test_swap_makes_new_chunks_queryable(self):
        """旧 active 写入旧 chunk → build shadow(新) → swap → 新可查旧不可查。"""
        await _reset_active()
        try:
            # 1) 初始 active 集合：chunk 1,2
            await asyncio.to_thread(
                vector_store.add_vectors,
                ids=_chunk_ids(2, 1),
                embeddings=[_vec(1), _vec(2)],
                documents=["旧A", "旧B"],
                metadatas=[{"doc_id": 1}, {"doc_id": 1}],
            )
            # 2) 影子：chunk 3,4（重灌后的新世界）
            cnt = await asyncio.to_thread(
                vector_store.build_shadow,
                _chunk_ids(2, 3),
                [_vec(3), _vec(4)],
                ["新A", "新B"],
                [{"doc_id": 1}, {"doc_id": 1}],
            )
            assert cnt == 2, "影子 count 应为 2"
            # swap 前：active 仍只有旧 chunk
            hits = await asyncio.to_thread(vector_store.query, _vec(1), None, 10)
            assert {h.chunk_id for h in hits} == {1, 2}, "swap 前 active 应为旧集合"
            # 3) swap
            await asyncio.to_thread(vector_store.swap_shadow_to_active)
            # 4) swap 后：active 只剩新 chunk
            hits = await asyncio.to_thread(vector_store.query, _vec(3), None, 10)
            assert {h.chunk_id for h in hits} == {3, 4}, "swap 后 active 应为新集合"
            assert vector_store.count() == 2
        finally:
            await _reset_active()

    async def test_shadow_failure_keeps_old_active_queryable(self):
        """影子写入中途失败（注入 build_shadow 抛异常）→ 旧 collection 原样可查。"""
        await _reset_active()
        try:
            await asyncio.to_thread(
                vector_store.add_vectors,
                ids=_chunk_ids(2, 1),
                embeddings=[_vec(1), _vec(2)],
                documents=["旧A", "旧B"],
                metadatas=[{"doc_id": 1}, {"doc_id": 1}],
            )
            # 注入失败：build_shadow 抛异常 → 旧 active 不动
            orig = vector_store.build_shadow

            def _boom(*a, **k):
                raise RuntimeError("注入: 影子写入失败")

            vector_store.build_shadow = _boom  # type: ignore[method-assign]
            try:
                with pytest.raises(RuntimeError):
                    await asyncio.to_thread(vector_store.build_shadow, [], [], [], [])
            finally:
                vector_store.build_shadow = orig  # type: ignore[method-assign]
            # 旧 active 仍可查
            hits = await asyncio.to_thread(vector_store.query, _vec(1), None, 10)
            assert {h.chunk_id for h in hits} == {1, 2}, "故障后旧集合必须可查"
        finally:
            await _reset_active()

    async def test_swap_then_query_sees_new_only(self):
        """swap 后全局查询（_get_collection 缓存已更新）只看到新 chunks。"""
        await _reset_active()
        try:
            await asyncio.to_thread(
                vector_store.add_vectors,
                ids=["100", "101"],
                embeddings=[_vec(100), _vec(101)],
                documents=["x", "y"],
                metadatas=[{"doc_id": 9}, {"doc_id": 9}],
            )
            await asyncio.to_thread(
                vector_store.build_shadow,
                ["200", "201"],
                [_vec(200), _vec(201)],
                ["新x", "新y"],
                [{"doc_id": 9}, {"doc_id": 9}],
            )
            await asyncio.to_thread(vector_store.swap_shadow_to_active)
            hits = await asyncio.to_thread(vector_store.query, _vec(200), None, 10)
            assert {h.chunk_id for h in hits} == {200, 201}
            assert vector_store.count() == 2
        finally:
            await _reset_active()
