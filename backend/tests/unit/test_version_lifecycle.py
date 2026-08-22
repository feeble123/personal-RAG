"""P0-8 版本生命周期：重灌成功/失败时版本状态机 + 旧版可用性。

直接调 manager（真 DB + fake embedding），验证：
- 重灌成功：target → active、旧版 retired、doc.active_version_id 更新、检索只见新 chunks
- 重灌中途失败（parse 抛异常）：旧版仍 active、旧 chunks 仍在、target=failed、检索不受影响
- 并发保护：第二个 reparse 被拒
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import delete, select

from app.db.models import Chunk, Document, DocumentVersion, KnowledgeBase
from app.db.session import async_session_factory
from app.modules.ingestion import manager
from app.services.chunker import Chunk as ChunkData, _hash

pytestmark = pytest.mark.asyncio

_cnt = {"n": 0}


async def _mk_doc() -> tuple[int, int]:
    """建一个库 + 一个文档（无版本），返回 (kb_id, doc_id)。"""
    _cnt["n"] += 1
    n = _cnt["n"]
    async with async_session_factory() as db:
        kb = KnowledgeBase(name=f"life库{n}", status="ready")
        db.add(kb)
        await db.flush()
        doc = Document(
            kb_id=kb.id, filename=f"life{n}.md", stored_path=f"life{n}.md",
            file_type="md", status="pending",
        )
        db.add(doc)
        await db.commit()
        return kb.id, doc.id


async def _mk_versions(doc_id: int, contents: list[str]) -> None:
    """手动建 1 个 active 版本 + chunks（模拟已发布的旧版）。"""
    async with async_session_factory() as db:
        doc = await db.get(Document, doc_id)
        ver = DocumentVersion(document_id=doc_id, status="active")
        db.add(ver)
        await db.flush()
        doc.active_version_id = ver.id
        for i, content in enumerate(contents):
            db.add(Chunk(
                kb_id=doc.kb_id, doc_id=doc_id, document_version_id=ver.id,
                chunk_index=i, content=content, section="1 旧章", page=1,
                content_hash=_hash(content),
            ))
        await db.commit()


async def _cleanup(kb_id: int) -> None:
    async with async_session_factory() as db:
        await db.execute(delete(KnowledgeBase).where(KnowledgeBase.id == kb_id))
        await db.commit()


class TestReparseSuccess:
    async def test_reparse_switches_active_version(self, client):
        """重灌成功：target → active、旧版 retired、检索只见新 chunks。"""
        kb_id, doc_id = await _mk_doc()
        try:
            await _mk_versions(doc_id, ["旧版内容一：明渠均匀流设计规范要求。"])

            # 重灌：创建 target 并写新 chunks（模拟 manager._write_chunks 后不发布）
            old_chunk_id = None
            async with async_session_factory() as db:
                old = (await db.execute(select(Chunk).where(Chunk.doc_id == doc_id))).scalars().all()
                assert len(old) == 1
                old_chunk_id = old[0].id
                doc = await db.get(Document, doc_id)
                target = DocumentVersion(document_id=doc.id, status="building")
                db.add(target)
                await db.flush()
                new_content = "新版内容二：防洪预案应急组织保障要求。"
                db.add(Chunk(
                    kb_id=doc.kb_id, doc_id=doc.id, document_version_id=target.id,
                    chunk_index=0, content=new_content, section="1 新章", page=1,
                    content_hash=_hash(new_content),
                ))
                target.chunk_count = 1
                await db.flush()
                # 发布
                await manager._publish_version(db, doc, target)
                await db.commit()

            async with async_session_factory() as db:
                doc = await db.get(Document, doc_id)
                assert doc.active_version_id == target.id
                # 旧版 retired、新版 active
                vers = (await db.execute(
                    select(DocumentVersion).where(DocumentVersion.document_id == doc_id)
                )).scalars().all()
                by_status = {v.status for v in vers}
                assert by_status == {"active", "retired"}
                # 旧版 chunks 仍在 DB（可回滚），只是不可查
                old = (await db.execute(select(Chunk).where(Chunk.id == old_chunk_id))).scalar()
                assert old is not None, "旧版 chunk 应保留（可回滚）"
                # 检索（_active_version_ids 过滤）只见新 chunks
                from app.services import rag

                active_ids = await rag._active_version_ids(db, doc_ids=[doc_id])
                assert active_ids == {target.id}
                new_chunks = (await db.execute(
                    select(Chunk).where(Chunk.document_version_id.in_(active_ids))
                )).scalars().all()
                assert len(new_chunks) == 1 and "新版" in new_chunks[0].content
        finally:
            await _cleanup(kb_id)


class TestReparseFailure:
    async def test_failure_keeps_old_active(self, client, monkeypatch):
        """重灌 parse 抛异常 → 旧版仍 active、旧 chunks 仍在、target=failed。"""
        kb_id, doc_id = await _mk_doc()
        try:
            await _mk_versions(doc_id, ["旧版内容一：明渠均匀流设计规范要求。"])

            # 注入：parser.parse 抛异常
            class _BoomParser:
                def parse(self, *a, **k):
                    raise RuntimeError("注入: 解析失败")

            def _get_parser(name):
                return _BoomParser()

            monkeypatch.setattr(manager, "get_parser", _get_parser)

            # 跑完整入库（应失败）
            await manager._run_ingestion(doc_id)
            await asyncio.sleep(0.1)  # 让任务回调落定

            async with async_session_factory() as db:
                doc = await db.get(Document, doc_id)
                assert doc.status == "failed", f"文档应标记 failed, 实得 {doc.status}"
                assert "解析失败" in (doc.error_message or "")
                # 旧版仍 active，target=failed
                vers = (await db.execute(
                    select(DocumentVersion).where(DocumentVersion.document_id == doc_id)
                )).scalars().all()
                assert len(vers) == 2, "应有一个旧版 + 一个失败 target"
                old_ver = next(v for v in vers if v.status == "active")
                failed_ver = next(v for v in vers if v.status == "failed")
                assert doc.active_version_id == old_ver.id, "pointer 应仍指向旧版"
                assert "解析失败" in (failed_ver.error_message or "")
                # 旧 chunks 仍在（检索数据没丢）
                chunks = (await db.execute(
                    select(Chunk).where(Chunk.document_version_id == old_ver.id)
                )).scalars().all()
                assert len(chunks) == 1 and "旧版" in chunks[0].content
        finally:
            await _cleanup(kb_id)


class TestReparseLease:
    async def test_reparse_conflict_409(self, client, admin_headers, sample_kb):
        """双 reparse：第二个在第一个 building 版本存在时返回 409。"""
        kb_id, doc_id = sample_kb
        # 手动插入一个 building 版本，模拟第一个重灌进行中
        async with async_session_factory() as db:
            db.add(DocumentVersion(document_id=doc_id, status="building"))
            await db.commit()
        r = await client.post(f"/api/admin/documents/{doc_id}/reparse", headers=admin_headers)
        assert r.status_code == 409, f"应有 building 版本 → 409, 实得 {r.status_code}"


class TestShadowFailureMatrix:
    """故障注入矩阵：embed / build_shadow / swap 任一阶段失败 → 旧版 active、旧 chunks 保留。"""

    async def _run_with_injection(self, client, admin_headers, sample_kb, monkeypatch, inject: str):
        kb_id, doc_id = sample_kb
        # 首次入库成功（旧版 active）
        from app.modules.ingestion import manager as mgr

        # 注入
        if inject == "embed":
            # 缓存强制全 miss（同内容重灌时 embedding 缓存会命中导致 embed 不触发），
            # 让 _write_chunks 必然走 embed_documents（已注入抛异常）
            async def _no_cache(db, hashes):
                return {}

            monkeypatch.setattr(mgr, "load_cache_vectors", _no_cache)

            async def _boom_embed(docs):
                raise RuntimeError("注入: embedding 失败")

            monkeypatch.setattr(mgr, "embed_documents", _boom_embed)
        elif inject == "build_shadow":
            def _boom_shadow(*a, **k):
                raise RuntimeError("注入: 影子索引写入失败")

            monkeypatch.setattr(mgr.vector_store, "build_shadow", _boom_shadow)
        elif inject == "swap":
            def _boom_swap():
                raise RuntimeError("注入: 影子切换失败")

            monkeypatch.setattr(mgr.vector_store, "swap_shadow_to_active", _boom_swap)
        else:
            raise ValueError(inject)

        # 直接触发一次新入库（重灌）
        r = await client.post(f"/api/admin/documents/{doc_id}/reparse", headers=admin_headers)
        assert r.status_code == 200, r.text
        # 让后台任务跑完失败路径（parse→embed→失败→标 target=failed）
        for _ in range(20):
            await asyncio.sleep(0.2)
            async with async_session_factory() as db:
                statuses = (await db.execute(
                    select(DocumentVersion.status).where(DocumentVersion.document_id == doc_id)
                )).scalars().all()
            if "failed" in statuses:
                break

        # 旧版仍 active、chunks 保留
        async with async_session_factory() as db:
            doc = await db.get(Document, doc_id)
            vers = (await db.execute(
                select(DocumentVersion).where(DocumentVersion.document_id == doc_id)
            )).scalars().all()
            active = [v for v in vers if v.status == "active"]
            assert len(active) == 1, f"应恰好 1 个 active, 实得 {len(active)}"
            old_chunks = (await db.execute(
                select(Chunk).where(Chunk.document_version_id == active[0].id)
            )).scalars().all()
            assert len(old_chunks) >= 1, "旧版 chunks 必须保留"
            # target 标 failed
            failed = [v for v in vers if v.status == "failed"]
            assert len(failed) == 1, f"应 1 个 failed target, 实得 {len(failed)}"
            assert doc.active_version_id == active[0].id

    @pytest.mark.parametrize("stage", ["embed", "build_shadow", "swap"])
    async def test_failure_matrix(self, client, admin_headers, sample_kb, monkeypatch, stage):
        await self._run_with_injection(client, admin_headers, sample_kb, monkeypatch, stage)
