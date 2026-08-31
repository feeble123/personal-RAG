"""单元 J 单元③：Celery 任务接入（幂等 + 双轨并存）。

覆盖：
- _claim_job_by_id：CAS 领指定 job，二次领返回 False（幂等，不重复处理）
- process_job_from_celery：对已终态/已领走的 job 直接返回（重复投递不重复干）
- enqueue_ingestion_async：use_celery=True 时投递 Celery 任务（send_task 被调用）；
  use_celery=False 时不投递（进程内 worker 兜底路径）

原则：Redis 只当传话筒，任务真相在 DB。测试全离线——不连真 Redis，
投递路径用 monkeypatch 记录 send_task 调用。
"""
from __future__ import annotations

import asyncio

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


async def _mk_doc() -> tuple[int, int]:
    _cnt["n"] += 1
    n = _cnt["n"]
    async with async_session_factory() as db:
        kb = KnowledgeBase(name=f"celery库{n}", status="ready")
        db.add(kb)
        await db.flush()
        doc = Document(
            kb_id=kb.id, filename=f"celery{n}.md", stored_path=f"celery{n}.md",
            file_type="md", status="pending",
        )
        db.add(doc)
        await db.commit()
        return kb.id, doc.id


async def _cleanup(kb_id: int) -> None:
    async with async_session_factory() as db:
        await db.execute(delete(KnowledgeBase).where(KnowledgeBase.id == kb_id))
        await db.commit()


async def _get_job(job_id: int) -> IngestionJob | None:
    async with async_session_factory() as db:
        return await db.get(IngestionJob, job_id)


class TestClaimJobById:
    async def test_claim_by_id_idempotent(self, client):
        """CAS 领指定 job：第一次抢到，第二次返回 False（不重复处理核心）。"""
        await _pause_worker()
        try:
            kb_id, doc_id = await _mk_doc()
            async with async_session_factory() as db:
                db.add(IngestionJob(document_id=doc_id, kind="ingest", stage="queued"))
                await db.commit()
                job_id = (await db.execute(
                    select(IngestionJob.id).where(IngestionJob.document_id == doc_id)
                )).scalar_one()

            first = await manager._claim_job_by_id(job_id)
            second = await manager._claim_job_by_id(job_id)
            assert first is True, "第一次应抢到"
            assert second is False, "同一 job 二次领应失败（幂等，不重复处理）"

            job = await _get_job(job_id)
            assert job.stage == "parsing"
            assert job.attempt == 1, "只被领一次，attempt 只 +1"
            assert job.lease_owner == "worker"
            await _cleanup(kb_id)
        finally:
            manager.start_worker()

    async def test_claim_by_id_nonexistent(self, client):
        """领不存在的 job：返回 False（不抛异常，幂等跳过）。"""
        assert await manager._claim_job_by_id(999999) is False


class TestProcessJobFromCelery:
    async def test_terminal_job_skipped(self, client):
        """已终态（succeeded）的 job：process_job_from_celery 直接返回，不重复处理。"""
        await _pause_worker()
        try:
            kb_id, doc_id = await _mk_doc()
            async with async_session_factory() as db:
                db.add(IngestionJob(document_id=doc_id, kind="ingest", stage="succeeded"))
                await db.commit()
                job_id = (await db.execute(
                    select(IngestionJob.id).where(IngestionJob.document_id == doc_id)
                )).scalar_one()

            # 终态 job 被 Celery 重复投递 → 不应报错、不应改状态
            await manager.process_job_from_celery(job_id)
            job = await _get_job(job_id)
            assert job.stage == "succeeded", "终态 job 不应被重新处理"
            await _cleanup(kb_id)
        finally:
            manager.start_worker()

    async def test_already_claimed_job_skipped(self, client):
        """已被人领走（parsing）的 job：process_job_from_celery 幂等跳过。"""
        await _pause_worker()
        try:
            kb_id, doc_id = await _mk_doc()
            async with async_session_factory() as db:
                db.add(IngestionJob(document_id=doc_id, kind="ingest", stage="parsing"))
                await db.commit()
                job_id = (await db.execute(
                    select(IngestionJob.id).where(IngestionJob.document_id == doc_id)
                )).scalar_one()

            await manager.process_job_from_celery(job_id)
            job = await _get_job(job_id)
            assert job.stage == "parsing", "进行中的 job 不应被重复处理"
            await _cleanup(kb_id)
        finally:
            manager.start_worker()


class TestDispatchToggle:
    async def test_use_celery_dispatches_task(self, client, monkeypatch):
        """use_celery=True：enqueue 落库后投递 Celery 任务（send_task 被调用一次）。"""
        await _pause_worker()
        try:
            kb_id, doc_id = await _mk_doc()
            calls: list[dict] = []
            monkeypatch.setattr(manager.settings, "use_celery", True)
            from app.core.celery_app import celery_app

            def _fake_send_task(name, args=None, queue=None):
                calls.append({"name": name, "args": args, "queue": queue})

            monkeypatch.setattr(celery_app, "send_task", _fake_send_task)

            job_id = await manager.enqueue_ingestion_async(doc_id, kind="ingest")
            assert job_id is not None
            assert len(calls) == 1, "use_celery=True 应投递一次 Celery 任务"
            assert calls[0]["name"] == "ingestion.process_job"
            assert calls[0]["args"] == [job_id]
            assert calls[0]["queue"] == "indexing"
            await _cleanup(kb_id)
        finally:
            manager.start_worker()

    async def test_no_celery_no_dispatch(self, client, monkeypatch):
        """use_celery=False（默认）：enqueue 不投递 Celery 任务（进程内 worker 兜底）。"""
        await _pause_worker()
        try:
            kb_id, doc_id = await _mk_doc()
            monkeypatch.setattr(manager.settings, "use_celery", False)
            from app.core.celery_app import celery_app

            calls: list = []
            monkeypatch.setattr(celery_app, "send_task", lambda *a, **k: calls.append(1))

            job_id = await manager.enqueue_ingestion_async(doc_id, kind="ingest")
            assert job_id is not None
            assert calls == [], "use_celery=False 不应投递 Celery 任务"
            await _cleanup(kb_id)
        finally:
            manager.start_worker()
