"""P0-9 单元3：worker 心跳 + reaper 回收 + 取消竞态。

覆盖：
- 心跳：worker 处理期间续租（lease_until 顺延），reaper 不误判存活 worker
- reaper：lease 过期且活跃的 job → 标 failed（LEASH_EXPIRED），doc 标 failed
- reaper 不回收：queued（无租约）/ lease 未过期 / 已终态
- 取消竞态：处理已完成后取消到达 → job 标 cancelled（不标 succeeded）
"""
from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import delete, select

from app.db.models import Document, IngestionJob, KnowledgeBase
from app.db.session import async_session_factory
from app.modules.ingestion import manager

pytestmark = pytest.mark.asyncio

_cnt = {"n": 0}


async def _pause_worker() -> None:
    manager.stop_worker()
    await asyncio.sleep(0.05)


async def _mk_doc(status: str = "pending") -> tuple[int, int]:
    _cnt["n"] += 1
    n = _cnt["n"]
    async with async_session_factory() as db:
        kb = KnowledgeBase(name=f"recovery库{n}", status="ready")
        db.add(kb)
        await db.flush()
        doc = Document(
            kb_id=kb.id, filename=f"recovery{n}.md", stored_path=f"recovery{n}.md",
            file_type="md", status=status,
        )
        db.add(doc)
        await db.commit()
        return kb.id, doc.id


async def _cleanup(kb_id: int) -> None:
    async with async_session_factory() as db:
        await db.execute(delete(KnowledgeBase).where(KnowledgeBase.id == kb_id))
        await db.commit()


async def _get_jobs(doc_id: int) -> list[IngestionJob]:
    async with async_session_factory() as db:
        return list((await db.execute(
            select(IngestionJob).where(IngestionJob.document_id == doc_id)
        )).scalars().all())


async def _mk_active_job(
    doc_id: int, *, stage: str = "parsing", lease_until: object | None = None,
    cancel_requested: bool = False, lease_owner: str | None = "worker",
) -> int:
    """手动建一个活跃 job（可指定租约/取消状态），返回 job_id。"""
    async with async_session_factory() as db:
        await db.execute(delete(IngestionJob).where(IngestionJob.document_id == doc_id))
        job = IngestionJob(
            document_id=doc_id, kind="ingest", stage=stage,
            lease_owner=lease_owner,
            lease_until=lease_until,
            cancel_requested=cancel_requested,
        )
        db.add(job)
        await db.commit()
        return job.id


class TestHeartbeat:
    async def test_heartbeat_renews_lease(self, client):
        """心跳：续租 lease_until 顺延 + heartbeat_at 更新。"""
        await _pause_worker()
        try:
            kb_id, doc_id = await _mk_doc()
            job_id = await _mk_active_job(doc_id, lease_until=None)

            async with async_session_factory() as db:
                await manager._heartbeat_job(db, job_id)
                await db.commit()
            jobs = await _get_jobs(doc_id)
            j = jobs[0]
            assert j.lease_until is not None, "心跳后应有 lease_until"
            assert j.heartbeat_at is not None, "心跳后应有 heartbeat_at"
            assert j.stage == "parsing", "心跳不动 stage"
            await _cleanup(kb_id)
        finally:
            manager.start_worker()

    async def test_heartbeat_persists_without_caller_commit(self, client):
        """心跳自己必须 commit 持久化——不依赖调用方手动 commit（单元 S bug：LEASE_EXPIRED）。

        回归背景：心跳协程每次新开独立会话，若不显式 commit，async with 退出时
        rollback，续租丢失 → reaper 误判 worker 死亡。此测试不复用手动 commit，
        直接从新会话读数据库，验证 lease_until 已被真正持久化。
        """
        await _pause_worker()
        try:
            kb_id, doc_id = await _mk_doc()
            job_id = await _mk_active_job(doc_id, lease_until=None)

            # 模拟心跳协程：新开独立会话调 _heartbeat_job（内部应自 commit）
            async with async_session_factory() as db:
                await manager._heartbeat_job(db, job_id)
                # 关键：不手动 commit，退出时若未提交即 rollback
            # 用全新的会话从数据库读，验证续租已真正落库
            async with async_session_factory() as db2:
                j = await db2.get(IngestionJob, job_id)
                assert j is not None, "心跳后 job 应存在"
                assert j.lease_until is not None, (
                    "心跳续租必须已 commit 持久化（单元 S bug 回归："
                    "未 commit 会 rollback，导致 lease 过期被 reaper 回收）"
                )
                assert j.heartbeat_at is not None, "心跳时间戳也应持久化"
                assert j.stage == "parsing", "心跳不动 stage"
            await _cleanup(kb_id)
        finally:
            manager.start_worker()

    async def test_heartbeat_skips_finished_job(self, client):
        """心跳：已终态（succeeded）的 job 不续租（worker 已完成，不误续）。"""
        await _pause_worker()
        try:
            kb_id, doc_id = await _mk_doc()
            async with async_session_factory() as db:
                job = IngestionJob(document_id=doc_id, kind="ingest", stage="succeeded")
                db.add(job)
                await db.commit()
                job_id = job.id
            async with async_session_factory() as db:
                await manager._heartbeat_job(db, job_id)
                await db.commit()
            jobs = await _get_jobs(doc_id)
            assert jobs[0].lease_until is None, "终态 job 不应续租"
            await _cleanup(kb_id)
        finally:
            manager.start_worker()


class TestReaper:
    async def test_reaper_reclaims_expired(self, client):
        """reaper：lease 过期且活跃 → job 标 failed（LEASE_EXPIRED），doc 标 failed。"""
        await _pause_worker()
        try:
            kb_id, doc_id = await _mk_doc(status="parsing")
            expired = manager._now() - timedelta(seconds=10)
            await _mk_active_job(doc_id, stage="embedding", lease_until=expired)

            await manager._reaper_pass()

            jobs = await _get_jobs(doc_id)
            assert jobs[0].stage == "failed", f"应标 failed, 实得 {jobs[0].stage}"
            assert jobs[0].error_code == "LEASE_EXPIRED"
            assert jobs[0].lease_owner is None, "回收后租约应清空"
            # doc 标 failed
            async with async_session_factory() as db:
                doc = await db.get(Document, doc_id)
                assert doc.status == "failed"
                assert "租约" in (doc.error_message or "")
            await _cleanup(kb_id)
        finally:
            manager.start_worker()

    async def test_reaper_keeps_healthy_lease(self, client):
        """reaper：lease 未过期（worker 存活）→ 不回收。"""
        await _pause_worker()
        try:
            kb_id, doc_id = await _mk_doc(status="parsing")
            future = manager._now() + timedelta(seconds=30)
            await _mk_active_job(doc_id, stage="embedding", lease_until=future)

            await manager._reaper_pass()

            jobs = await _get_jobs(doc_id)
            assert jobs[0].stage == "embedding", f"存活 worker 不应被回收, 实得 {jobs[0].stage}"
            assert jobs[0].error_code is None
            await _cleanup(kb_id)
        finally:
            manager.start_worker()

    async def test_reaper_ignores_queued_no_lease(self, client):
        """reaper：queued（无租约，等 worker 领）→ 不回收。"""
        await _pause_worker()
        try:
            kb_id, doc_id = await _mk_doc(status="pending")
            await _mk_active_job(doc_id, stage="queued", lease_owner=None, lease_until=None)

            await manager._reaper_pass()

            jobs = await _get_jobs(doc_id)
            assert jobs[0].stage == "queued", "queued 无租约不应被回收"
            await _cleanup(kb_id)
        finally:
            manager.start_worker()

    async def test_reaper_ignores_finished(self, client):
        """reaper：已终态（succeeded/failed）→ 不回收。"""
        await _pause_worker()
        try:
            kb_id, doc_id = await _mk_doc(status="ready")
            async with async_session_factory() as db:
                job = IngestionJob(
                    document_id=doc_id, kind="ingest", stage="succeeded",
                    lease_until=manager._now() - timedelta(seconds=10),  # 过期但已终态
                )
                db.add(job)
                await db.commit()
            await manager._reaper_pass()
            jobs = await _get_jobs(doc_id)
            assert jobs[0].stage == "succeeded", "已终态不应被回收"
            await _cleanup(kb_id)
        finally:
            manager.start_worker()


class TestCancelRace:
    async def test_cancel_after_publish_marks_cancelled(self, client, monkeypatch):
        """取消竞态：处理已完成（publish 已发），取消请求随后到达 → job 标 cancelled。

        模拟：job 已到 succeeded 前一刻，cancel_requested 被置位 → _execute_job 成功
        路径看到 cancel_requested → 不标 succeeded，标 cancelled。
        """
        await _pause_worker()
        try:
            kb_id, doc_id = await _mk_doc(status="ready")
            # 造一个「刚处理完但用户取消」的 job（cancel_requested=True, stage 仍活跃）
            job_id = await _mk_active_job(
                doc_id, stage="publishing", cancel_requested=True, lease_until=None,
            )
            # 注入 _process_document 快速返回（模拟处理已完成，避免真实跑解析）
            async def _fast_process(doc_id):  # noqa: ARG001
                pass

            monkeypatch.setattr(manager, "_process_document", _fast_process)
            await manager._execute_job(job_id)

            jobs = await _get_jobs(doc_id)
            assert jobs[0].stage == "cancelled", f"取消竞态应标 cancelled, 实得 {jobs[0].stage}"
            assert jobs[0].error_code == "CANCELLED"
            # doc 回 pending（可重入重灌）
            async with async_session_factory() as db:
                doc = await db.get(Document, doc_id)
                assert doc.status == "pending"
            await _cleanup(kb_id)
        finally:
            manager.start_worker()

    async def test_cancel_requested_during_processing_raises(self, client, monkeypatch):
        """取消竞态：处理中被取消 → _raise_if_cancelled 抛 _JobCancelled → job cancelled。"""
        await _pause_worker()
        try:
            kb_id, doc_id = await _mk_doc(status="parsing")
            job_id = await _mk_active_job(doc_id, stage="embedding", cancel_requested=True)

            async with async_session_factory() as db:
                with pytest.raises(manager._JobCancelled):
                    await manager._raise_if_cancelled(db, doc_id)

            # 直接执行失败路径：取消 → job cancelled + doc pending
            async def _boom(doc_id):  # noqa: ARG001
                raise manager._JobCancelled("用户取消入库")

            monkeypatch.setattr(manager, "_process_document", _boom)
            await manager._execute_job(job_id)

            jobs = await _get_jobs(doc_id)
            assert jobs[0].stage == "cancelled", f"处理中取消应标 cancelled, 实得 {jobs[0].stage}"
            async with async_session_factory() as db:
                doc = await db.get(Document, doc_id)
                assert doc.status == "pending", "doc 应回 pending 可重入"
            await _cleanup(kb_id)
        finally:
            manager.start_worker()
