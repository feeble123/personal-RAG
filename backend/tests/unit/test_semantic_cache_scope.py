"""P0-3 语义缓存作用域加固单元测试：跨用户隔离 + TTL 过期不命中。

直接测 semantic_cache.find/store（真 DB + 假向量），不依赖 HTTP 链路。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.db.models import SemanticCache
from app.db.session import async_session_factory
from app.services import semantic_cache

pytestmark = pytest.mark.asyncio

QVEC = [0.1, 0.2, 0.3, 0.4]
SUBJECT = "明渠均匀流"
KB = 7


async def _cleanup() -> None:
    from sqlalchemy import delete

    async with async_session_factory() as db:
        await db.execute(delete(SemanticCache))
        await db.commit()


class TestUserScope:
    async def test_same_user_same_scope_hits(self, client):
        """同一用户同作用域 → 命中（正向对照：用户隔离不破坏正常命中）。"""
        try:
            async with async_session_factory() as db:
                await semantic_cache.store(
                    db, QVEC, SUBJECT, "答案A", [], kb_id=KB, doc_scope=None, style=None, user_id=1
                )
            async with async_session_factory() as db:
                hit = await semantic_cache.find(
                    db, QVEC, SUBJECT, kb_id=KB, doc_scope=None, style=None, user_id=1
                )
            assert hit is not None and hit[0] == "答案A"
        finally:
            await _cleanup()

    async def test_cross_user_no_hit(self, client):
        """同一问题同一库，不同用户 → 不命中（P0-3 跨用户隔离）。"""
        try:
            async with async_session_factory() as db:
                await semantic_cache.store(
                    db, QVEC, SUBJECT, "答案A", [], kb_id=KB, user_id=1
                )
            async with async_session_factory() as db:
                hit = await semantic_cache.find(
                    db, QVEC, SUBJECT, kb_id=KB, user_id=2
                )
            assert hit is None
        finally:
            await _cleanup()

    async def test_null_legacy_rows_not_hit(self, client):
        """历史存量（user_id=NULL）→ 保守不命中（任何用户都不重放旧条目）。"""
        try:
            async with async_session_factory() as db:
                await semantic_cache.store(
                    db, QVEC, SUBJECT, "旧答案", [], kb_id=KB, user_id=None
                )
            async with async_session_factory() as db:
                hit = await semantic_cache.find(
                    db, QVEC, SUBJECT, kb_id=KB, user_id=1
                )
            assert hit is None
        finally:
            await _cleanup()


class TestTTL:
    async def test_expired_not_hit(self, client):
        """超出 TTL 的条目 → 不命中（P0-3 防重灌后重放旧答案）。"""
        try:
            async with async_session_factory() as db:
                await semantic_cache.store(
                    db, QVEC, SUBJECT, "旧答案", [], kb_id=KB, user_id=1
                )
            # 回拨 updated_at 至 2 天前（默认 TTL=86400s=1 天）
            async with async_session_factory() as db:
                row = (await db.execute(
                    __import__("sqlalchemy").select(SemanticCache)
                )).scalars().first()
                row.updated_at = datetime.now() - timedelta(days=2)
                await db.commit()
            async with async_session_factory() as db:
                hit = await semantic_cache.find(
                    db, QVEC, SUBJECT, kb_id=KB, user_id=1
                )
            assert hit is None
        finally:
            await _cleanup()

    async def test_fresh_hits(self, client):
        """未过期的条目照常命中（TTL 不误伤正常缓存）。"""
        try:
            async with async_session_factory() as db:
                await semantic_cache.store(
                    db, QVEC, SUBJECT, "新答案", [], kb_id=KB, user_id=1
                )
            async with async_session_factory() as db:
                hit = await semantic_cache.find(
                    db, QVEC, SUBJECT, kb_id=KB, user_id=1
                )
            assert hit is not None and hit[0] == "新答案"
        finally:
            await _cleanup()
