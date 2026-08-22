"""P0-5 引用不可变快照：重灌/删文档删除 chunk 后，历史引用行保留（chunk_id 置 NULL，快照可显示）。

直接验证 DB 层 ON DELETE SET NULL 行为 + 重放路径的 chunk 存在性守卫。
"""
from __future__ import annotations

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from app.db.models import Chunk, Citation, Conversation, Document, DocumentVersion, KnowledgeBase, Message, User
from app.db.session import async_session_factory
from app.modules.qa.routes import _existing_chunk_ids

pytestmark = pytest.mark.asyncio

_cnt = {"n": 0}


async def _mk_scene() -> dict:
    _cnt["n"] += 1
    n = _cnt["n"]
    async with async_session_factory() as db:
        user = User(username=f"citeuser{n}", password_hash="x", role="user")
        db.add(user)
        await db.flush()
        conv = Conversation(user_id=user.id)
        db.add(conv)
        await db.flush()
        msg = Message(conversation_id=conv.id, role="assistant", content="答案", is_complete=True)
        db.add(msg)
        await db.flush()
        kb = KnowledgeBase(name=f"cite库{n}", status="ready")
        db.add(kb)
        await db.flush()
        doc = Document(kb_id=kb.id, filename="规范.pdf", stored_path=f"c{n}.pdf", file_type="pdf", status="ready")
        db.add(doc)
        await db.flush()
        ver = DocumentVersion(document_id=doc.id, status="active")
        db.add(ver)
        await db.flush()
        doc.active_version_id = ver.id
        chunk = Chunk(
            kb_id=kb.id, doc_id=doc.id, document_version_id=ver.id, chunk_index=0,
            content="## 5 应急保障\n引用原文内容", section="5 应急保障", page=12,
            content_hash=f"citehash{n}",
        )
        db.add(chunk)
        await db.flush()
        cite = Citation(
            message_id=msg.id, chunk_id=chunk.id, kb_id=kb.id, doc_id=doc.id,
            source="规范.pdf", page=12, section="5 应急保障", snippet="引用原文内容",
            score=0.9, rank=1,
        )
        db.add(cite)
        await db.commit()
        return {
            "user": user.id, "conv": conv.id, "msg": msg.id, "kb": kb.id,
            "doc": doc.id, "chunk": chunk.id, "cite": cite.id,
        }


async def _cleanup(s: dict) -> None:
    async with async_session_factory() as db:
        await db.execute(delete(User).where(User.id == s["user"]))
        await db.execute(delete(KnowledgeBase).where(KnowledgeBase.id == s["kb"]))
        await db.commit()


class TestCitationSnapshot:
    async def test_chunk_delete_keeps_citation(self, client):
        """重灌删 chunk（模拟 _write_chunks 的 delete）→ 引用行保留、chunk_id=NULL、快照完整。"""
        s = await _mk_scene()
        try:
            async with async_session_factory() as db:
                await db.execute(delete(Chunk).where(Chunk.doc_id == s["doc"]))
                await db.commit()
                row = await db.get(Citation, s["cite"])
                assert row is not None, "引用行不应被级联删除"
                assert row.chunk_id is None
                assert row.source == "规范.pdf" and "引用原文" in row.snippet
                assert row.doc_id == s["doc"] and row.section == "5 应急保障"
        finally:
            await _cleanup(s)

    async def test_doc_delete_keeps_citation(self, client):
        """删除文档（级联删 chunks）→ 引用行保留（快照可显示）。"""
        s = await _mk_scene()
        try:
            async with async_session_factory() as db:
                await db.execute(delete(Document).where(Document.id == s["doc"]))
                await db.commit()
                row = await db.get(Citation, s["cite"])
                assert row is not None
                assert row.chunk_id is None and row.snippet == "引用原文内容"
        finally:
            await _cleanup(s)

    async def test_valid_chunk_id_kept(self, client):
        """chunk 未被删时，引用正常回链真实 chunk_id。"""
        s = await _mk_scene()
        try:
            async with async_session_factory() as db:
                row = await db.get(Citation, s["cite"])
                assert row.chunk_id == s["chunk"]
        finally:
            await _cleanup(s)


class TestReplayGuard:
    async def test_stale_chunk_id_insert_fails_without_guard(self, client):
        """指向已删 chunk 的非空 chunk_id 直接 INSERT → 外键违约（证明守卫必要）。"""
        s = await _mk_scene()
        try:
            async with async_session_factory() as db:
                await db.execute(delete(Chunk).where(Chunk.doc_id == s["doc"]))
                await db.commit()
            async with async_session_factory() as db:
                with pytest.raises(IntegrityError):
                    db.add(
                        Citation(message_id=s["msg"], chunk_id=s["chunk"], source="规范.pdf", snippet="x")
                    )
                    await db.flush()
                await db.rollback()
        finally:
            await _cleanup(s)

    async def test_guard_sets_null_for_missing_chunk(self, client):
        """用 _existing_chunk_ids 守卫后：缺失 chunk_id → 置 None，快照照常落库。"""
        s = await _mk_scene()
        try:
            async with async_session_factory() as db:
                await db.execute(delete(Chunk).where(Chunk.doc_id == s["doc"]))
                await db.commit()
            async with async_session_factory() as db:
                valid = await _existing_chunk_ids(db, [s["chunk"]])
                cid = s["chunk"] if s["chunk"] in valid else None
                assert cid is None  # 已删 → 置 NULL
                cite2 = Citation(message_id=s["msg"], chunk_id=cid, source="规范.pdf", snippet="快照仍可显示")
                db.add(cite2)
                await db.commit()
                row2 = await db.get(Citation, cite2.id)
                assert row2.chunk_id is None and row2.snippet == "快照仍可显示"
        finally:
            await _cleanup(s)

    async def test_guard_keeps_valid_chunk(self, client):
        """chunk 仍存在时守卫保留真实 chunk_id。"""
        s = await _mk_scene()
        try:
            async with async_session_factory() as db:
                valid = await _existing_chunk_ids(db, [s["chunk"]])
                assert s["chunk"] in valid
        finally:
            await _cleanup(s)
