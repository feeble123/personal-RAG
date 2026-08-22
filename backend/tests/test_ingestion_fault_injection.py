"""P0-9 单元4：故障注入矩阵（worker kill / 进程重启恢复 / 重复投递幂等）。

验收目标（P0-9）：
- 重启后无永久卡在 parsing/indexing 的任务（reaper 回收）
- 同一 job 最多发布一次（CAS + 幂等）
- worker kill 后旧 active 版本可用率 100%（P0-8 兼容）

场景：
1. worker kill：真实入库 → 注入租约过期（模拟 worker 死亡）→ reaper 回收 →
   job 标 failed(LEASE_EXPIRED)、doc 标 failed、旧 active 版本仍可查
2. 进程重启恢复：遗留 parsing/embedding 幽灵 job（无 lease）→ reaper 视为可回收？
   ——注意：无租约的活跃 job 不回收（queued 逻辑），但「有租约且过期」才回收。
   这里验证「启动 worker 后幽灵任务被清理，不再永久卡死」
3. 重复投递：同一文档连续两次 reparse/upload → 只有一个活跃 job（幂等）
"""
from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import delete, select

from app.db.models import Chunk, Document, DocumentVersion, IngestionJob, KnowledgeBase
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
        kb = KnowledgeBase(name=f"fault库{n}", status="ready")
        db.add(kb)
        await db.flush()
        doc = Document(
            kb_id=kb.id, filename=f"fault{n}.md", stored_path=f"fault{n}.md",
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


class TestWorkerKillRecovery:
    async def test_worker_kill_reclaims_and_keeps_old_active(self, client, admin_headers, sample_kb):
        """worker kill：真实入库后模拟 worker 死亡（租约过期）→ reaper 回收 → 旧版可查。"""
        kb_id, doc_id = sample_kb
        # sample_kb 已 ready（旧 active 版本 + succeeded job）
        async with async_session_factory() as db:
            doc = await db.get(Document, doc_id)
            old_ver = doc.active_version_id
            assert old_ver is not None

        # 先等 sample_kb 的 ingest job 到终态（doc ready 时 job 可能还在 publishing）
        for _ in range(40):
            jobs = await _get_jobs(doc_id)
            if jobs and jobs[0].stage in ("succeeded", "failed", "cancelled"):
                break
            await asyncio.sleep(0.2)
        assert jobs and jobs[0].stage == "succeeded", f"ingest job 应先 succeeded, 实得 {jobs[0].stage if jobs else None}"

        await _pause_worker()
        try:
            # 停 worker 后 reparse：新 job 停在 queued（没人领）——模拟「worker 在领走前就死了」
            r = await client.post(f"/api/admin/documents/{doc_id}/reparse", headers=admin_headers)
            assert r.status_code == 200, r.text
            jobs = await _get_jobs(doc_id)
            new_job = next(j for j in jobs if j.kind == "reparse")
            assert new_job.stage == "queued", "worker 已停，新 job 应停在 queued"

            # 模拟 worker 领走后死亡：手动推进到活跃 stage + 租约过期
            async with async_session_factory() as db:
                job = await db.get(IngestionJob, new_job.id)
                job.stage = "parsing"
                job.lease_owner = "dead-worker"
                job.lease_until = manager._now() - timedelta(seconds=10)
                doc = await db.get(Document, doc_id)
                doc.status = "parsing"
                await db.commit()

            # reaper 回收
            await manager._reaper_pass()

            # 新 job 被回收标 failed(LEASE_EXPIRED)
            jobs = await _get_jobs(doc_id)
            reclaimed = next(j for j in jobs if j.kind == "reparse")
            assert reclaimed.stage == "failed", f"应标 failed, 实得 {reclaimed.stage}"
            assert reclaimed.error_code == "LEASE_EXPIRED"
            # 旧 active 版本仍可查（pointer 未动）
            async with async_session_factory() as db:
                doc = await db.get(Document, doc_id)
                assert doc.active_version_id == old_ver, "旧版 pointer 不应被回收影响"
                # 旧版 chunks 仍在
                chunks = (await db.execute(
                    select(Chunk).where(Chunk.document_version_id == old_ver)
                )).scalars().all()
                assert len(chunks) >= 1, "旧版 chunks 应保留"
        finally:
            manager.start_worker()
            # sample_kb 由 fixture teardown 清理，这里不重复删


class TestRestartNoStuckJobs:
    async def test_restart_reclaims_ghost_jobs(self, client, admin_headers):
        """进程重启：遗留 parsing/embedding 幽灵 job（有租约但过期）→ 启动后 reaper 清理。"""
        kb_id, doc_id = await _mk_doc(status="parsing")
        try:
            # 模拟进程中断遗留：job 停在 parsing，租约过期（worker 已死）
            async with async_session_factory() as db:
                job = IngestionJob(
                    document_id=doc_id, kind="ingest", stage="parsing",
                    lease_owner="dead-worker",
                    lease_until=manager._now() - timedelta(seconds=30),
                )
                db.add(job)
                await db.commit()

            # 模拟进程重启：worker 重新启动，第一轮 reaper 清理幽灵任务
            await _pause_worker()
            manager.start_worker()
            # 等 reaper 跑一轮（worker 循环每轮先 reaper）
            for _ in range(40):
                jobs = await _get_jobs(doc_id)
                if jobs and jobs[0].stage in ("failed", "cancelled"):
                    break
                await asyncio.sleep(0.2)
            manager.stop_worker()
            await asyncio.sleep(0.05)

            jobs = await _get_jobs(doc_id)
            assert jobs and jobs[0].stage == "failed", f"幽灵任务应被回收, 实得 {jobs and jobs[0].stage}"
            assert jobs[0].error_code == "LEASE_EXPIRED"
            # doc 不再永久卡 parsing
            async with async_session_factory() as db:
                doc = await db.get(Document, doc_id)
                assert doc.status == "failed", "幽灵文档应标 failed 而非永久 parsing"
        finally:
            manager.start_worker()
            await _cleanup(kb_id)


class TestDuplicateDispatch:
    async def test_double_upload_single_job(self, client, admin_headers):
        """重复投递：同一文档连续两次 enqueue → 只有一个活跃 job（幂等）。"""
        kb_id, doc_id = await _mk_doc(status="pending")
        try:
            # 连续两次投递（同文档）
            j1 = await manager.enqueue_ingestion_async(doc_id, kind="ingest")
            j2 = await manager.enqueue_ingestion_async(doc_id, kind="ingest")
            assert j1 is not None, "第一次应创建 job"
            assert j2 is None, "第二次应被幂等拦截（已有活跃 job）"
            jobs = await _get_jobs(doc_id)
            assert len(jobs) == 1, f"应只有 1 个 job, 实得 {len(jobs)}"
            assert jobs[0].stage == "queued"
        finally:
            manager.start_worker()
            await _cleanup(kb_id)

    async def test_reparse_while_active_409_or_single(self, client, admin_headers):
        """重灌幂等：已有一个活跃 job 时再 reparse → 不产生第二个活跃 job。"""
        kb_id, doc_id = await _mk_doc(status="pending")
        try:
            await _pause_worker()
            try:
                await manager.enqueue_ingestion_async(doc_id, kind="ingest")
                # 已有 queued job 再 enqueue → 跳过
                j2 = await manager.enqueue_ingestion_async(doc_id, kind="reparse")
                assert j2 is None, "已有活跃 job 时再投递应跳过"
                jobs = await _get_jobs(doc_id)
                assert len(jobs) == 1, f"应只有 1 个 job, 实得 {len(jobs)}"
            finally:
                manager.start_worker()
        finally:
            await _cleanup(kb_id)
