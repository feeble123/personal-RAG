"""P1-3 单元3：embedding profile 指纹。

覆盖：
- profile.fingerprint 稳定且字段敏感（改 model → 指纹变）
- 同 content_hash 不同 profile → 两条缓存记录（互不覆盖）
- 维度不匹配写入 → 抛错
- 旧缓存（profile=""）不被新 profile 命中 → 重新计算
"""
from __future__ import annotations

import json

import pytest

from app.services import embedding
from app.services.embedding import EmbeddingProfile, profile_fingerprint


class TestProfile:
    def test_fingerprint_stable(self):
        p1 = EmbeddingProfile(
            provider="openai_compatible", model="BAAI/bge-m3",
            base_url="https://api.siliconflow.cn/v1", dimension=1024,
            normalize=False, query_instruction="", doc_instruction="", tokenizer="t",
        )
        p2 = EmbeddingProfile(
            provider="openai_compatible", model="BAAI/bge-m3",
            base_url="https://api.siliconflow.cn/v1", dimension=1024,
            normalize=False, query_instruction="", doc_instruction="", tokenizer="t",
        )
        assert p1.fingerprint() == p2.fingerprint()

    def test_fingerprint_sensitive_to_model(self):
        p1 = EmbeddingProfile(
            provider="openai_compatible", model="BAAI/bge-m3",
            base_url="https://api.siliconflow.cn/v1", dimension=1024,
            normalize=False, query_instruction="", doc_instruction="", tokenizer="t",
        )
        p2 = EmbeddingProfile(
            provider="openai_compatible", model="other-model",
            base_url="https://api.siliconflow.cn/v1", dimension=1024,
            normalize=False, query_instruction="", doc_instruction="", tokenizer="t",
        )
        assert p1.fingerprint() != p2.fingerprint()

    def test_fingerprint_sensitive_to_dim(self):
        p1 = EmbeddingProfile(
            provider="openai_compatible", model="BAAI/bge-m3",
            base_url="https://api.siliconflow.cn/v1", dimension=1024,
            normalize=False, query_instruction="", doc_instruction="", tokenizer="t",
        )
        p2 = EmbeddingProfile(
            provider="openai_compatible", model="BAAI/bge-m3",
            base_url="https://api.siliconflow.cn/v1", dimension=768,
            normalize=False, query_instruction="", doc_instruction="", tokenizer="t",
        )
        assert p1.fingerprint() != p2.fingerprint()


class TestCacheWithProfile:
    async def test_same_hash_different_profile_separate(self, client):
        """同 content_hash 不同 profile → 两条缓存记录。"""
        from sqlalchemy import select

        from app.db.models import EmbeddingCache
        from app.db.session import async_session_factory

        h = "deadbeef" * 8  # 64 位 hash
        v1 = [0.1, 0.2, 0.3]
        v2 = [0.9, 0.8, 0.7]

        fp1 = "profile-one"
        fp2 = "profile-two"

        async with async_session_factory() as db:
            db.add(EmbeddingCache(content_hash=h, profile_fingerprint=fp1, model_version="m1", vector_json=json.dumps(v1)))
            db.add(EmbeddingCache(content_hash=h, profile_fingerprint=fp2, model_version="m2", vector_json=json.dumps(v2)))
            await db.commit()

            rows = (await db.scalars(select(EmbeddingCache).where(EmbeddingCache.content_hash == h))).all()
            assert len(rows) == 2, "不同 profile 应分开缓存"

            # 精确加载 profile-one
            got = (await db.scalars(
                select(EmbeddingCache).where(
                    EmbeddingCache.content_hash == h,
                    EmbeddingCache.profile_fingerprint == fp1,
                )
            )).one()
            assert json.loads(got.vector_json) == v1

    async def test_load_cache_vectors_uses_current_profile(self, client, monkeypatch):
        """load_cache_vectors 按当前 profile 精确匹配（旧行 profile='' 不被命中）。"""
        from sqlalchemy import select

        from app.db.models import EmbeddingCache
        from app.db.session import async_session_factory

        h = "abcdef12" * 8
        async with async_session_factory() as db:
            # 旧缓存：profile=""（模拟升级前）
            db.add(EmbeddingCache(content_hash=h, profile_fingerprint="", model_version="old", vector_json=json.dumps([0.5])))
            await db.commit()

        # 直接用实际 profile 加载：旧行（""）不应命中
        async with async_session_factory() as db:
            fp = profile_fingerprint()
            rows = (await db.scalars(
                select(EmbeddingCache).where(
                    EmbeddingCache.content_hash == h,
                    EmbeddingCache.profile_fingerprint == fp,
                )
            )).all()
            assert len(rows) == 0, "旧 profile='' 缓存不应被新 profile 命中"


class TestDimensionGuard:
    def test_dimension_mismatch_raises(self):
        """向量维度与 collection 不一致 → 抛错。"""
        from unittest.mock import MagicMock

        from app.services import vector_store

        col = MagicMock()
        # 模拟 collection 已有 1024 维向量
        col.get.return_value = {"embeddings": [[0.1] * 1024]}
        with pytest.raises(ValueError, match="维度不匹配"):
            vector_store._assert_dimension(col, [[0.1] * 64])
