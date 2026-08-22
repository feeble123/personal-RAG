"""P0-7 chunk 身份：同内容跨文档保留独立 chunk；embedding 缓存复用；重灌互不牵连。

直接调 manager._write_chunks（真 DB + fake embedding + Chroma 重建），验证 DB 层结果。
"""
from __future__ import annotations

import pytest
from sqlalchemy import delete, func, select

from app.db.models import Chunk, Document, DocumentVersion, EmbeddingCache, KnowledgeBase
from app.db.session import async_session_factory
from app.modules.ingestion import manager
from app.services.chunker import Chunk as ChunkData, _hash

pytestmark = pytest.mark.asyncio

_cnt = {"n": 0}
# 两份文档共用完全相同的正文（跨文档同内容场景）
_SHARED_CONTENT = "## 5 应急保障\n完全相同的条文内容：明渠均匀流的形成条件与设计规范要求。"


def _chunk():
    return ChunkData(
        content=_SHARED_CONTENT, section="5 应急保障", page=1, content_hash=_hash(_SHARED_CONTENT)
    )


async def _setup():
    _cnt["n"] += 1
    n = _cnt["n"]
    async with async_session_factory() as db:
        kb = KnowledgeBase(name=f"chunk身份库{n}", status="ready")
        db.add(kb)
        await db.flush()
        kb_id = kb.id
        doc_a = Document(
            kb_id=kb_id, filename="文档A.md", stored_path=f"a{n}.md", file_type="md", status="pending"
        )
        doc_b = Document(
            kb_id=kb_id, filename="文档B.md", stored_path=f"b{n}.md", file_type="md", status="pending"
        )
        db.add_all([doc_a, doc_b])
        await db.commit()
        return kb_id, doc_a.id, doc_b.id


async def _write(kb_id: int, doc_id: int, chunks) -> None:
    async with async_session_factory() as db:
        kb = await db.get(KnowledgeBase, kb_id)
        doc = await db.get(Document, doc_id)
        # P0-8：创建 target 版本，chunks 挂到版本下
        target = DocumentVersion(document_id=doc.id, status="building")
        db.add(target)
        await db.flush()
        await manager._write_chunks(db, doc, target, chunks)
        await db.commit()  # _write_chunks 只 flush，提交由调用方负责（这里补上）


async def _doc_chunks(doc_id: int) -> list[Chunk]:
    async with async_session_factory() as db:
        return (await db.execute(select(Chunk).where(Chunk.doc_id == doc_id))).scalars().all()


async def _cleanup(kb_id: int) -> None:
    async with async_session_factory() as db:
        await db.execute(delete(KnowledgeBase).where(KnowledgeBase.id == kb_id))
        await db.commit()


class TestCrossDocIdentity:
    async def test_identical_content_kept_per_doc(self, client):
        """同库两份文档含同一条文 → 各自独立 chunk；embedding 缓存只算一次；重灌互不牵连。"""
        kb_id, doc_a, doc_b = await _setup()
        try:
            await _write(kb_id, doc_a, [_chunk()])
            await _write(kb_id, doc_b, [_chunk()])
            ca = await _doc_chunks(doc_a)
            cb = await _doc_chunks(doc_b)
            assert len(ca) == 1 and len(cb) == 1  # 各自独立 chunk，不再归并
            assert ca[0].content_hash == cb[0].content_hash  # 同内容
            # embedding 缓存：同一 content_hash 只调一次 API（跨文档复用）
            async with async_session_factory() as db:
                n = (
                    await db.execute(
                        select(func.count())
                        .select_from(EmbeddingCache)
                        .where(EmbeddingCache.content_hash == ca[0].content_hash)
                    )
                ).scalar()
            assert n == 1
            # 重灌 doc_a → doc_b 的该条文 chunk 仍在
            await _write(kb_id, doc_a, [_chunk()])
            cb_after = await _doc_chunks(doc_b)
            assert len(cb_after) == 1
        finally:
            await _cleanup(kb_id)

    async def test_identical_chunks_within_doc_kept(self, client):
        """同文档内两条相同内容（OCR 噪声场景）→ 都保留，复合唯一 (doc_id, chunk_index) 不冲突。"""
        kb_id, doc_a, _ = await _setup()
        try:
            await _write(kb_id, doc_a, [_chunk(), _chunk()])
            rows = await _doc_chunks(doc_a)
            assert len(rows) == 2  # 同内容两处 occurrence 都保留
            assert rows[0].chunk_index != rows[1].chunk_index
        finally:
            await _cleanup(kb_id)
