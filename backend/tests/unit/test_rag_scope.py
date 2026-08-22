"""P0-2 检索 scope 隔离单元测试：跨库污染修复。

两 KB 各有同名《防汛预案》与同名「5 应急保障」章节 → 验证书名解析 / 枚举扩展 /
章节扩展 / 整文档补全在 kb 过滤后只返回当前库的切片（B 的候选分数再高也不混入）。
"""
from __future__ import annotations

import pytest

from app.db.models import Chunk, Document, DocumentVersion, KnowledgeBase
from app.db.session import async_session_factory
from app.services import rag
from app.services.rag import RetrievedChunk

_counter = {"n": 0}

pytestmark = pytest.mark.asyncio


async def _seed_two_kbs():
    """建 KB-A / KB-B，各含同名《防汛预案.pdf》文档 + 同名「5 应急保障」章节 5 个切片。"""
    _counter["n"] += 1
    n = _counter["n"]
    async with async_session_factory() as db:
        kb_a = KnowledgeBase(name=f"scope库A{n}", status="ready")
        kb_b = KnowledgeBase(name=f"scope库B{n}", status="ready")
        db.add_all([kb_a, kb_b])
        await db.flush()

        doc_a = Document(
            kb_id=kb_a.id, filename="防汛预案.pdf", stored_path=f"a{n}.pdf",
            file_type="pdf", status="ready",
        )
        doc_b = Document(
            kb_id=kb_b.id, filename="防汛预案.pdf", stored_path=f"b{n}.pdf",
            file_type="pdf", status="ready",
        )
        db.add_all([doc_a, doc_b])
        await db.flush()

        # P0-8：每文档建一个 active 版本，chunk 挂到版本下
        for doc in (doc_a, doc_b):
            ver = DocumentVersion(document_id=doc.id, status="active")
            db.add(ver)
            await db.flush()
            doc.active_version_id = ver.id

        # 各自 5 个同名「5 应急保障」章节切片（正文内容不同，可区分来源；≥min_content_len=40）
        for kb, doc, tag in ((kb_a, doc_a, "A"), (kb_b, doc_b, "B")):
            ver = await db.get(DocumentVersion, doc.active_version_id)
            for i in range(5):
                db.add(
                    Chunk(
                        kb_id=kb.id,
                        doc_id=doc.id,
                        document_version_id=ver.id,
                        chunk_index=i,
                        content=(
                            f"## 5 应急保障\n{tag}库内容第{i}条："
                            f"应急组织保障与通信保障要求，确保应急预案有效实施执行。{i}"
                        ),
                        section="5 应急保障",
                        page=10 + i,
                        content_hash=f"h{n}-{tag}-{i}",
                    )
                )
        await db.commit()
        return kb_a.id, kb_b.id, doc_a.id, doc_b.id


async def _cleanup(kb_a: int, kb_b: int) -> None:
    async with async_session_factory() as db:
        from sqlalchemy import delete

        await db.execute(delete(KnowledgeBase).where(KnowledgeBase.id.in_([kb_a, kb_b])))
        await db.commit()


class TestTitleResolutionScoped:
    async def test_same_name_doc_restricted_to_kb(self, client):
        """两库同名文档：点名《防汛预案》在 KB-A 只返回 A 的 doc。"""
        kb_a, kb_b, doc_a, doc_b = await _seed_two_kbs()
        try:
            q = "《防汛预案》中应急保障的要求有哪些"
            async with async_session_factory() as db:
                assert await rag.resolve_documents_by_title(db, q, kb_id=kb_a) == [doc_a]
                assert await rag.resolve_documents_by_title(db, q, kb_id=kb_b) == [doc_b]
                # 跨全部库模式（kb_id=None）：同名文档每候选命中其一（既有去重行为，非本次改动）
                both = await rag.resolve_documents_by_title(db, q)
                assert both and set(both) <= {doc_a, doc_b}
        finally:
            await _cleanup(kb_a, kb_b)

    async def test_unknown_doc_in_kb_returns_empty(self, client):
        """KB-A 里点名只有 KB-B 才有的文档 → 返回空（不跨库取 B）。"""
        kb_a, kb_b, doc_a, doc_b = await _seed_two_kbs()
        try:
            # 把 B 的文档改名，使「唯一文档」只在 B
            async with async_session_factory() as db:
                from sqlalchemy import update

                await db.execute(
                    update(Document)
                    .where(Document.id == doc_b)
                    .values(filename="独有文档B.pdf")
                )
                await db.commit()
            q = "《独有文档B》中应急保障的要求有哪些"
            async with async_session_factory() as db:
                assert await rag.resolve_documents_by_title(db, q, kb_id=kb_a) == []
                assert await rag.resolve_documents_by_title(db, q, kb_id=kb_b) == [doc_b]
        finally:
            await _cleanup(kb_a, kb_b)


class TestEnumerationExpansionScoped:
    async def test_enumeration_only_current_kb(self, client):
        """枚举扩展：B 库同名章节候选分数更高 → kb 过滤后仍只返回 A 的切片。"""
        kb_a, kb_b, doc_a, doc_b = await _seed_two_kbs()
        try:
            async with async_session_factory() as db:
                a_chunks = (
                    await db.execute(
                        rag.select(Chunk.id).where(Chunk.doc_id == doc_a).order_by(Chunk.id)
                    )
                ).scalars().all()
                b_chunks = (
                    await db.execute(
                        rag.select(Chunk.id).where(Chunk.doc_id == doc_b).order_by(Chunk.id)
                    )
                ).scalars().all()
            # B 候选分数远高于 A（若不过滤会被 B 抢走）
            cand = [(cid, 0.9) for cid in b_chunks] + [(cid, 0.5) for cid in a_chunks]
            section_by_id = {cid: "5 应急保障" for cid in a_chunks + b_chunks}
            async with async_session_factory() as db:
                out = await rag._expand_enumeration_sections(
                    db, "应急保障有哪些要求", cand, section_by_id, top_k=5, kb_id=kb_a
                )
            assert out, "枚举扩展应命中"
            out_ids = [cid for cid, _ in out]
            assert out_ids, "扩展结果非空"
            assert all(cid in set(a_chunks) for cid in out_ids), "返回了其他库的切片"
            assert not any(cid in set(b_chunks) for cid in out_ids), "B 库切片混入扩展"
        finally:
            await _cleanup(kb_a, kb_b)


class TestChapterExpansionScoped:
    async def test_chapter_only_current_kb(self, client, monkeypatch):
        """章节扩展：kb 过滤后只取当前库整章子节，B 库同名章节不混入。"""
        kb_a, kb_b, doc_a, doc_b = await _seed_two_kbs()
        try:
            import app.core.config as config_mod
            from app.services import rag as rag_mod

            monkeypatch.setattr(config_mod.settings, "rerank_enabled", True)
            async with async_session_factory() as db:
                a_chunks = (
                    await db.execute(
                        rag.select(Chunk.id).where(Chunk.doc_id == doc_a).order_by(Chunk.id)
                    )
                ).scalars().all()
                b_chunks = (
                    await db.execute(
                        rag.select(Chunk.id).where(Chunk.doc_id == doc_b).order_by(Chunk.id)
                    )
                ).scalars().all()

            async def _stub_rerank(_q, docs):
                return [0.9] * len(docs)

            monkeypatch.setattr(rag_mod, "rerank", _stub_rerank)
            section_by_id = {cid: "5 应急保障" for cid in a_chunks + b_chunks}
            cand = [(a_chunks[0], 0.9)]
            async with async_session_factory() as db:
                out = await rag._expand_chapter_sections(
                    db, "应急保障有哪些要求", cand, section_by_id, top_k=5, kb_id=kb_a
                )
            assert out, "章节扩展应命中"
            out_ids = [cid for cid, _ in out]
            assert all(cid in set(a_chunks) for cid in out_ids), "返回了其他库的切片"
            assert not any(cid in set(b_chunks) for cid in out_ids), "B 库切片混入扩展"
        finally:
            await _cleanup(kb_a, kb_b)


class TestDocumentWideScoped:
    async def test_document_wide_only_doc_kb(self, client):
        """整文档补全：只返回目标文档所在 KB 的切片。"""
        kb_a, kb_b, doc_a, doc_b = await _seed_two_kbs()
        try:
            cites = [
                RetrievedChunk(
                    chunk_id=1, kb_id=kb_a, doc_id=doc_a,
                    source="防汛预案.pdf", section="5 应急保障", snippet="x",
                )
            ]
            async with async_session_factory() as db:
                out = await rag.retrieve_document_wide(
                    db, "应急保障", kb_id=kb_a, _cites=cites, cap=100
                )
            assert out, "整文档补全应命中"
            assert all(c.doc_id == doc_a for c in out), "补全混入其他文档"
            assert all(c.kb_id == kb_a for c in out), "补全跨库"
        finally:
            await _cleanup(kb_a, kb_b)
