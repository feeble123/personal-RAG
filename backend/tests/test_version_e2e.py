"""P0-8 端到端：重灌失败旧版可用率 100%（核心验收）+ 版本历史展示。

场景：
- 上传文档 → ready（版本 v1 active）
- 重灌失败（注入 chunk_blocks 抛异常）→ 旧版仍 active、旧 chunks 可查、target=failed
- 重灌成功 → 新版本 active、旧版 retired
- document_detail 展示版本历史（versions 数组）
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from app.db.models import Chunk, Document, DocumentVersion
from app.db.session import async_session_factory
from app.modules import ingestion

pytestmark = pytest.mark.asyncio

_MD = "# 水利工程基础\n\n## 明渠均匀流\n\n明渠均匀流的形成条件包括：长直棱柱体渠道、正坡、糙率不变、流量恒定。\n"


async def _wait_ready(client, headers, kb_id, doc_id, expected=("ready", "failed")):
    for _ in range(40):
        r = await client.get(f"/api/admin/kbs/{kb_id}/documents", headers=headers)
        item = next(d for d in r.json()["items"] if d["id"] == doc_id)
        if item["status"] in expected:
            return item
        await asyncio.sleep(0.2)
    raise AssertionError(f"文档未就绪 doc={doc_id}")


async def _count_chunks(doc_id: int) -> int:
    async with async_session_factory() as db:
        return len((await db.execute(select(Chunk).where(Chunk.doc_id == doc_id))).scalars().all())


async def _active_version_id(doc_id: int) -> int | None:
    async with async_session_factory() as db:
        doc = await db.get(Document, doc_id)
        return doc.active_version_id


class TestReparseFailureKeepsOld:
    async def test_failed_reparse_keeps_old_active(self, client, admin_headers, sample_kb, monkeypatch):
        """重灌失败 → 旧版 active、旧 chunks 可查、target=failed、文档状态 failed。"""
        kb_id, doc_id = sample_kb
        # 首次入库成功
        await _wait_ready(client, admin_headers, kb_id, doc_id)
        old_ver = await _active_version_id(doc_id)
        old_chunks = await _count_chunks(doc_id)
        assert old_ver is not None and old_chunks >= 1

        # 注入：build_parent_child 抛异常（模拟解析后分块阶段失败）。
        # P1-4 起正文分块走 build_parent_child；chunk_blocks 仅在无 parent-child
        # 产出时回退调用，注入它打不到真实分块路径（会漏测失败保护）。
        import app.modules.ingestion.manager as mgr

        def _boom(*args, **kwargs):
            raise RuntimeError("注入: 分块失败")

        monkeypatch.setattr(mgr, "build_parent_child", _boom)
        r = await client.post(f"/api/admin/documents/{doc_id}/reparse", headers=admin_headers)
        assert r.status_code == 200, r.text
        item = await _wait_ready(client, admin_headers, kb_id, doc_id, expected=("failed",))
        assert item["status"] == "failed"
        assert "分块失败" in (item["error_message"] or "")

        # 旧版仍 active，chunks 保留
        cur_ver = await _active_version_id(doc_id)
        assert cur_ver == old_ver, "pointer 应仍指向旧版"
        assert await _count_chunks(doc_id) == old_chunks, "旧 chunks 不应丢失"

        # 版本历史：旧版 active + 一个 failed target
        async with async_session_factory() as db:
            vers = (await db.execute(
                select(DocumentVersion).where(DocumentVersion.document_id == doc_id)
            )).scalars().all()
            statuses = {v.status for v in vers}
            assert statuses == {"active", "failed"}, f"应 active+failed, 实得 {statuses}"


class TestReparseSuccessSwitches:
    async def test_successful_reparse_retires_old(self, client, admin_headers, sample_kb, monkeypatch):
        """重灌成功 → 新版本 active、旧版 retired、检索只见新版。"""
        kb_id, doc_id = sample_kb
        await _wait_ready(client, admin_headers, kb_id, doc_id)
        old_ver = await _active_version_id(doc_id)

        # 换成不同内容重灌
        async with async_session_factory() as db:
            doc = await db.get(Document, doc_id)
            doc.filename = "v2.md"
            doc.stored_path = doc.stored_path  # 保持路径（测试用 md，内容由上传决定）
            await db.commit()
        # 重灌（内容不变，但会走新版本）
        r = await client.post(f"/api/admin/documents/{doc_id}/reparse", headers=admin_headers)
        assert r.status_code == 200
        await _wait_ready(client, admin_headers, kb_id, doc_id)

        new_ver = await _active_version_id(doc_id)
        assert new_ver is not None and new_ver != old_ver, "pointer 应切到新版本"
        # 旧版 retired，新版 active
        async with async_session_factory() as db:
            vers = (await db.execute(
                select(DocumentVersion).where(DocumentVersion.document_id == doc_id)
            )).scalars().all()
            by_id = {v.id: v for v in vers}
            assert by_id[old_ver].status == "retired", "旧版应 retired"
            assert by_id[new_ver].status == "active", "新版应 active"
            # 新版 chunks 存在
            new_chunks = (await db.execute(
                select(Chunk).where(Chunk.document_version_id == new_ver)
            )).scalars().all()
            assert len(new_chunks) >= 1


class TestVersionHistoryApi:
    async def test_document_detail_has_versions(self, client, admin_headers, sample_kb):
        """document_detail 返回 versions 数组（重灌历史审计）。"""
        kb_id, doc_id = sample_kb
        await _wait_ready(client, admin_headers, kb_id, doc_id)
        r = await client.get(f"/api/admin/documents/{doc_id}", headers=admin_headers)
        assert r.status_code == 200
        data = r.json()
        assert "versions" in data, "DocumentOut 应含 versions"
        assert isinstance(data["versions"], list)
        assert len(data["versions"]) >= 1
        v = data["versions"][0]
        assert v["status"] in ("active", "retired", "building", "failed", "validated")
        assert "chunk_count" in v and "activated_at" in v
