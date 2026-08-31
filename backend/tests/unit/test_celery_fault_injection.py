"""单元 J 单元④：Celery 幂等 + 重试 + 故障测试（acks_late 场景）。

验收目标：
- acks_late=True 已生效（worker 跑完才回执，崩溃不丢 job）
- 重发同一 job 不重复入库（CAS 幂等 → chunk 数不翻倍）
- worker 崩溃 → reaper 回收 → 重灌成功（不丢活）

原则：Redis 只当传话筒，真相在 DB。测试全离线（fake embedding / 临时 SQLite），
不连真 Redis——「杀 worker」用租约过期模拟（与 P0-9 故障注入矩阵同款手法）。
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import delete, func, select

from app.db.models import Chunk, Document, IngestionJob, KnowledgeBase
from app.db.session import async_session_factory
from app.modules.ingestion import manager

pytestmark = pytest.mark.asyncio

_cnt = {"n": 0}


async def _pause_worker() -> None:
    manager.stop_worker()
    await asyncio.sleep(0.05)


async def _get_jobs(doc_id: int) -> list[IngestionJob]:
    async with async_session_factory() as db:
        return list((await db.execute(
            select(IngestionJob).where(IngestionJob.document_id == doc_id)
        )).scalars().all())


async def _active_chunk_count(doc_id: int) -> int:
    """某文档 active 版本的 chunk 总数（判断是否重复入库）。"""
    async with async_session_factory() as db:
        return (await db.scalar(
            select(func.count()).select_from(Chunk)
            .join(Document, Chunk.doc_id == Document.id)
            .where(Document.id == doc_id, Chunk.document_version_id == Document.active_version_id)
        )) or 0


class TestAcksLateConfig:
    async def test_acks_late_enabled(self):
        """acks_late=True 已生效（计划要求：只给已证明幂等的入库任务开）。"""
        from app.modules.ingestion.celery_tasks import process_ingestion_job

        assert process_ingestion_job.acks_late is True, "入库任务必须 acks_late=True（幂等可安全重投）"
        assert process_ingestion_job.name == "ingestion.process_job"


class TestNoDuplicatePublish:
    async def test_redeliver_after_success_no_duplicate(self, client, admin_headers, sample_kb):
        """重发已成功 job（模拟 acks_late 下崩溃后 Celery 重投）：不重复入库。

        worker 干完但没回执（崩溃）→ Celery 重投同一 job_id → CAS 抢不到（succeeded）
        → 跳过 → active chunk 数不变。
        """
        kb_id, doc_id = sample_kb
        # sample_kb 已 ready，等其 ingest job 到终态
        for _ in range(40):
            jobs = await _get_jobs(doc_id)
            if jobs and jobs[0].stage in ("succeeded", "failed", "cancelled"):
                break
            await asyncio.sleep(0.2)
        assert jobs and jobs[0].stage == "succeeded", "ingest job 应先 succeeded"
        job_id = jobs[0].id
        before = await _active_chunk_count(doc_id)
        assert before >= 1, "入库后应有 chunk"

        # 模拟 Celery 重投同一 job（崩溃没回执 → 重投）
        await _pause_worker()
        try:
            await manager.process_job_from_celery(job_id)
        finally:
            manager.start_worker()

        # 重投后 chunk 数不变（幂等，不重复入库）
        after = await _active_chunk_count(doc_id)
        assert after == before, f"重投不应重复入库: before={before} after={after}"
        # job 状态仍 succeeded（未被重新处理）
        jobs = await _get_jobs(doc_id)
        assert jobs[0].stage == "succeeded"


class TestCrashReclaimRerun:
    async def test_crash_then_reparse_succeeds(self, client, admin_headers, sample_kb):
        """worker 崩溃 → reaper 回收 → 重灌成功（不丢活）。

        模拟：reparse 后 worker 领走又崩溃（租约过期）→ reaper 回收标 failed →
        再 reparse（重投）→ 走 _execute_job 完整入库 → succeeded + doc ready。
        """
        kb_id, doc_id = sample_kb
        # 等 ingest 到终态
        for _ in range(40):
            jobs = await _get_jobs(doc_id)
            if jobs and jobs[0].stage in ("succeeded", "failed", "cancelled"):
                break
            await asyncio.sleep(0.2)

        await _pause_worker()
        try:
            # 1) reparse 造 queued job（worker 停了，没人领）
            r = await client.post(f"/api/admin/documents/{doc_id}/reparse", headers=admin_headers)
            assert r.status_code == 200, r.text
            jobs = await _get_jobs(doc_id)
            new_job = next(j for j in jobs if j.kind == "reparse")
            assert new_job.stage == "queued"

            # 2) 模拟 worker 领走后崩溃：租约过期
            from datetime import timedelta

            async with async_session_factory() as db:
                job = await db.get(IngestionJob, new_job.id)
                job.stage = "parsing"
                job.lease_owner = "dead-celery-worker"
                job.lease_until = manager._now() - timedelta(seconds=10)
                await db.commit()

            # 3) reaper 回收 → failed(LEASE_EXPIRED)
            await manager._reaper_pass()
            jobs = await _get_jobs(doc_id)
            reclaimed = next(j for j in jobs if j.id == new_job.id)
            assert reclaimed.stage == "failed", f"应标 failed, 实得 {reclaimed.stage}"
            assert reclaimed.error_code == "LEASE_EXPIRED"

            # 4) 用户重灌（重投）→ 完整入库成功（不丢活）
            j3 = await manager.enqueue_ingestion_async(doc_id, kind="reparse")
            assert j3 is not None, "崩溃回收后重灌应能投递新 job"
            # 直接驱动执行（等价 Celery worker 领走处理）
            await manager.process_job_from_celery(j3)
            jobs = await _get_jobs(doc_id)
            rerun = next(j for j in jobs if j.id == j3)
            assert rerun.stage == "succeeded", f"重灌应成功, 实得 {rerun.stage}"
        finally:
            manager.start_worker()
