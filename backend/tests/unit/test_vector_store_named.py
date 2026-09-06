"""单元 B1：vector_store 按名字操作 collection 的软切换原语。

验证「每库每版本一个 collection」的基础能力（真实 Chroma，临时目录隔离）：
- build_collection 建指定名字、互不牵连（建 B 不影响 A）
- query_named 只查指定名字的 collection
- drop_collection 删除指定名字、不影响其他
- 同名字重建（幂等，清残留）
"""
from __future__ import annotations

import asyncio
import math

import pytest

from app.services import vector_store

pytestmark = pytest.mark.asyncio

_DIM = 8


def _vec(seed: int) -> list[float]:
    raw = [float((seed * 31 + i * 7) % 17) / 17.0 for i in range(_DIM)]
    norm = math.sqrt(sum(v * v for v in raw)) or 1.0
    return [v / norm for v in raw]


def _ids(n: int, offset: int = 0) -> list[str]:
    return [str(offset + i) for i in range(n)]


async def _cleanup(*names: str):
    for n in names:
        vector_store.drop_collection(n)


class TestNamedCollection:
    async def test_build_and_query_named(self):
        """build_collection 建 A → query_named 只查 A，别的名字查不到。"""
        name = "kb_1_v1"
        await _cleanup(name)
        try:
            cnt = await asyncio.to_thread(
                vector_store.build_collection,
                name,
                _ids(2, 1),
                [_vec(1), _vec(2)],
                ["A1", "A2"],
                [{"kb_id": 1}, {"kb_id": 1}],
            )
            assert cnt == 2
            hits = await asyncio.to_thread(vector_store.query_named, name, _vec(1), None, 10)
            assert {h.chunk_id for h in hits} == {1, 2}
            # 别的名字查不到
            assert await asyncio.to_thread(vector_store.count_named, "kb_1_v9") == 0
        finally:
            await _cleanup(name, "kb_1_v9")

    async def test_collections_independent(self):
        """两个名字的 collection 互不牵连：建 B 不影响 A 的内容。"""
        a, b = "kb_1_v1", "kb_2_v1"
        await _cleanup(a, b)
        try:
            await asyncio.to_thread(
                vector_store.build_collection, a, _ids(2, 1), [_vec(1), _vec(2)], ["a1", "a2"], [{"kb_id": 1}, {"kb_id": 1}]
            )
            await asyncio.to_thread(
                vector_store.build_collection, b, _ids(2, 100), [_vec(100), _vec(101)], ["b1", "b2"], [{"kb_id": 2}, {"kb_id": 2}]
            )
            hits_a = await asyncio.to_thread(vector_store.query_named, a, _vec(1), None, 10)
            hits_b = await asyncio.to_thread(vector_store.query_named, b, _vec(100), None, 10)
            assert {h.chunk_id for h in hits_a} == {1, 2}
            assert {h.chunk_id for h in hits_b} == {100, 101}
        finally:
            await _cleanup(a, b)

    async def test_rebuild_same_name_is_idempotent(self):
        """同名字重建：清残留后重写，count 反映新内容（软切换的幂等前提）。"""
        name = "kb_1_v1"
        await _cleanup(name)
        try:
            await asyncio.to_thread(
                vector_store.build_collection, name, _ids(3, 1), [_vec(1), _vec(2), _vec(3)], ["x"] * 3, [{"kb_id": 1}] * 3
            )
            assert await asyncio.to_thread(vector_store.count_named, name) == 3
            # 同名字重建为 2 条
            await asyncio.to_thread(
                vector_store.build_collection, name, _ids(2, 1), [_vec(1), _vec(2)], ["y"] * 2, [{"kb_id": 1}] * 2
            )
            assert await asyncio.to_thread(vector_store.count_named, name) == 2
        finally:
            await _cleanup(name)

    async def test_drop_named_isolated(self):
        """drop 一个名字不影响另一个。"""
        a, b = "kb_1_v1", "kb_2_v1"
        await _cleanup(a, b)
        try:
            await asyncio.to_thread(
                vector_store.build_collection, a, _ids(2, 1), [_vec(1), _vec(2)], ["a1", "a2"], [{"kb_id": 1}, {"kb_id": 1}]
            )
            await asyncio.to_thread(
                vector_store.build_collection, b, _ids(2, 100), [_vec(100), _vec(101)], ["b1", "b2"], [{"kb_id": 2}, {"kb_id": 2}]
            )
            await asyncio.to_thread(vector_store.drop_collection, a)
            assert await asyncio.to_thread(vector_store.count_named, a) == 0
            assert await asyncio.to_thread(vector_store.count_named, b) == 2
        finally:
            await _cleanup(a, b)

    async def test_get_embeddings_by_ids_named(self):
        """按名字取向量。"""
        name = "kb_1_v1"
        await _cleanup(name)
        try:
            await asyncio.to_thread(
                vector_store.build_collection,
                name,
                _ids(2, 1),
                [_vec(1), _vec(2)],
                ["A1", "A2"],
                [{"kb_id": 1}, {"kb_id": 1}],
            )
            embs = await asyncio.to_thread(vector_store.get_embeddings_by_ids_named, name, [1, 2])
            assert set(embs) == {1, 2}
            assert len(embs[1]) == _DIM
        finally:
            await _cleanup(name)
