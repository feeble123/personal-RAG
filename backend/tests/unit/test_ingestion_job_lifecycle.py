"""P0-9 单元2：job 生命周期 + 路由改造。

覆盖：
- enqueue → DB job（queued），CAS 领任务（并发不重复领）
- worker 执行后 job 到 succeeded、doc 到 ready
- 取消：queued 未领 → 直接 cancelled；进行中 → cancel_requested 协作式中断
- 路由：GET /admin/jobs 列表 + POST /documents/{id}/cancel

注意：worker 常驻在 lifespan 里，会跟手动建 job 的测试抢任务。因此纯逻辑测试
（CAS/取消）用 `_pause_worker()` 暂时停掉 worker，测完再 `start_worker()` 恢复。
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
    """停掉 worker，等它退出（避免轮询抢测试里手动建的 job）。"""
    manager.stop_worker()
    await asyncio.sleep(0.05)


async def _mk_doc(status: str = "pending") -> tuple[int, int]:
    """建一个库 + 一个文档，返回 (kb_id, doc_id)。"""
    _cnt["n"] += 1
    n = _cnt["n"]
    async with async_session_factory() as db:
        kb = KnowledgeBase(name=f"joblife库{n}", status="ready")
        db.add(kb)
        await db.flush()
        doc = Document(
            kb_id=kb.id, filename=f"joblife{n}.md", stored_path=f"joblife{n}.md",
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


async def _get_doc(doc_id: int) -> Document | None:
    async with async_session_factory() as db:
        return await db.get(Document, doc_id)


class TestClaimCAS:
    async def test_concurrent_claim_no_duplicate(self, client):
        """并发领同一 queued job：只有一人领到（CAS 防重复处理核心）。"""
        await _pause_worker()
        try:
            kb_id, doc_id = await _mk_doc()
            async with async_session_factory() as db:
                await db.execute(delete(IngestionJob))
                await db.commit()
                db.add(IngestionJob(document_id=doc_id, kind="ingest", stage="queued"))
                await db.commit()

            r1, r2 = await asyncio.gather(
                manager._claim_next_job_async(), manager._claim_next_job_async()
            )
            claimed = [x for x in (r1, r2) if x is not None]
            # 同一 job，至少一人领到；绝不允许两人都返回「领到」（CAS 保证 rowcount 判定）
            assert len(claimed) >= 1, "至少一人应领到"
            assert len(set(claimed)) == len(claimed), "不应重复领同一 job"
            # 领到后，job 只剩一个 parsing（未被重复处理）
            jobs = await _get_jobs(doc_id)
            assert len(jobs) == 1 and jobs[0].stage == "parsing"
            await _cleanup(kb_id)
        finally:
            manager.start_worker()

    async def test_claim_picks_oldest_and_marks_parsing(self, client):
        """CAS 领任务：按 id 先到先得，领到的 job stage→parsing + attempt+1 + lease。"""
        await _pause_worker()
        try:
            kb_id, doc_id = await _mk_doc()
            async with async_session_factory() as db:
                await db.execute(delete(IngestionJob))
                await db.commit()
                db.add(IngestionJob(document_id=doc_id, kind="ingest", stage="queued"))
                await db.commit()

            claimed = await manager._claim_next_job_async()
            assert claimed is not None, "应领到 queued job"

            jobs = await _get_jobs(doc_id)
            assert len(jobs) == 1
            j = jobs[0]
            assert j.stage == "parsing", f"领到后应为 parsing, 实得 {j.stage}"
            assert j.attempt == 1, f"attempt 应 +1, 实得 {j.attempt}"
            assert j.lease_owner == "worker"
            assert j.lease_until is not None, "应设置租约到期时间"
            assert j.heartbeat_at is not None

            # 再领 → 没有 queued，返回 None
            assert await manager._claim_next_job_async() is None
            await _cleanup(kb_id)
        finally:
            manager.start_worker()

    async def test_claim_not_duplicate_after_cas(self, client):
        """CAS 领任务不重复：领走一个 queued 后不能再领它（worker 幂等）。"""
        await _pause_worker()
        try:
            kb_id, doc_id = await _mk_doc()
            async with async_session_factory() as db:
                await db.execute(delete(IngestionJob))
                await db.commit()
                db.add(IngestionJob(document_id=doc_id, kind="ingest", stage="queued"))
                db.add(IngestionJob(document_id=doc_id, kind="ingest", stage="queued"))
                await db.commit()

            # 顺序领：第一次领到 job1（最早），第二次领到 job2，第三次无 queued
            c1 = await manager._claim_next_job_async()
            c2 = await manager._claim_next_job_async()
            c3 = await manager._claim_next_job_async()
            assert c1 is not None and c2 is not None
            assert c1 != c2, "两个不同 job 应各被领一次"
            assert c3 is None, "没有 queued 后应返回 None（不重复领）"

            jobs = await _get_jobs(doc_id)
            assert all(j.stage == "parsing" for j in jobs), "领到后都应 parsing"
            await _cleanup(kb_id)
        finally:
            manager.start_worker()


class TestLifecycleViaWorker:
    async def test_upload_job_reaches_succeeded(self, client, admin_headers, sample_kb):
        """API 上传 → worker 处理 → job succeeded + doc ready（真实端到端）。"""
        kb_id, doc_id = sample_kb
        # sample_kb 已等到 doc ready，但 job 可能还在 publishing（doc ready 与 job
        # succeeded 不在同一原子点）。轮询 job 到终态。
        for _ in range(40):
            jobs = await _get_jobs(doc_id)
            if jobs and jobs[0].stage in ("succeeded", "failed", "cancelled"):
                break
            await asyncio.sleep(0.2)
        jobs = await _get_jobs(doc_id)
        assert len(jobs) == 1, f"应恰好 1 条 job, 实得 {len(jobs)}"
        assert jobs[0].stage == "succeeded", f"job 应 succeeded, 实得 {jobs[0].stage}"
        doc = await _get_doc(doc_id)
        assert doc is not None and doc.status == "ready"
        assert doc.active_version_id is not None

    async def test_reparse_creates_new_job_kind(self, client, admin_headers, sample_kb):
        """reparse 创建 kind=reparse 的 job，处理后又回到 ready。"""
        kb_id, doc_id = sample_kb
        r = await client.post(f"/api/admin/documents/{doc_id}/reparse", headers=admin_headers)
        assert r.status_code == 200, r.text

        # 等 job 终态
        for _ in range(40):
            jobs = await _get_jobs(doc_id)
            if any(j.stage == "succeeded" for j in jobs if j.kind == "reparse"):
                break
            await asyncio.sleep(0.2)
        jobs = await _get_jobs(doc_id)
        reparse_job = [j for j in jobs if j.kind == "reparse"]
        assert len(reparse_job) == 1, f"应有 1 条 reparse job, 实得 {len(reparse_job)}"
        assert reparse_job[0].stage == "succeeded"
        doc = await _get_doc(doc_id)
        assert doc.status == "ready"


class TestCancel:
    async def test_cancel_queued_job(self, client):
        """取消尚未被领的 queued job：直接标 cancelled，doc 回 pending。"""
        await _pause_worker()
        try:
            kb_id, doc_id = await _mk_doc()
            async with async_session_factory() as db:
                await db.execute(delete(IngestionJob))
                await db.commit()
            await manager.enqueue_ingestion_async(doc_id, kind="ingest")
            ok = await manager.cancel_ingestion(doc_id)
            assert ok is True

            jobs = await _get_jobs(doc_id)
            assert len(jobs) == 1
            assert jobs[0].stage == "cancelled", f"queued 取消应直接 cancelled, 实得 {jobs[0].stage}"
            doc = await _get_doc(doc_id)
            assert doc.status == "pending"

            # worker 不应领走已 cancelled 的 job
            assert await manager._claim_next_job_async() is None
            await _cleanup(kb_id)
        finally:
            manager.start_worker()

    async def test_cancel_inflight_sets_flag_and_raises(self, client):
        """进行中取消：cancel_requested 置位 → _raise_if_cancelled 抛 _JobCancelled。"""
        await _pause_worker()
        try:
            kb_id, doc_id = await _mk_doc()
            async with async_session_factory() as db:
                await db.execute(delete(IngestionJob))
                await db.commit()
            job_id = await manager.enqueue_ingestion_async(doc_id, kind="ingest")
            # 领走（模拟 worker 已开始）
            claimed = await manager._claim_next_job_async()
            assert claimed == job_id

            # 用户点取消 → cancel_requested=True（进行中不直接 cancelled）
            assert await manager.cancel_ingestion(doc_id) is True
            jobs = await _get_jobs(doc_id)
            assert jobs[0].cancel_requested is True, "进行中取消应置 cancel_requested"
            assert jobs[0].stage == "parsing", "进行中不直接改终态"

            # 批次边界检查 → 抛 _JobCancelled（干净中断）
            async with async_session_factory() as db:
                with pytest.raises(manager._JobCancelled):
                    await manager._raise_if_cancelled(db, doc_id)
            await _cleanup(kb_id)
        finally:
            manager.start_worker()


class TestJobRoutes:
    async def test_list_jobs_returns_items(self, client, admin_headers, sample_kb):
        """GET /admin/jobs 返回 job 列表（含 stage/attempt/error）。"""
        r = await client.get("/api/admin/jobs", headers=admin_headers)
        assert r.status_code == 200, r.text
        data = r.json()
        assert "total" in data and isinstance(data["items"], list)
        assert data["total"] >= 1, "至少有一条 sample_kb 产生的 job"
        first = data["items"][0]
        for field in ("id", "document_id", "kind", "stage", "attempt", "created_at"):
            assert field in first, f"job 列表缺字段 {field}"

    async def test_cancel_endpoint(self, client, admin_headers, sample_kb):
        """POST /documents/{id}/cancel 返回 cancelled 布尔。"""
        kb_id, doc_id = sample_kb
        # 无活跃 job（已 succeeded）→ cancelled=False
        r = await client.post(f"/api/admin/documents/{doc_id}/cancel", headers=admin_headers)
        assert r.status_code == 200, r.text
        assert r.json()["cancelled"] is False

        # 造一个 queued job → cancelled=True（停 worker 避免抢先领走）
        await _pause_worker()
        try:
            async with async_session_factory() as db:
                await db.execute(delete(IngestionJob).where(IngestionJob.document_id == doc_id))
                await db.commit()
            await manager.enqueue_ingestion_async(doc_id, kind="ingest")
            r = await client.post(f"/api/admin/documents/{doc_id}/cancel", headers=admin_headers)
            assert r.status_code == 200
            assert r.json()["cancelled"] is True
        finally:
            manager.start_worker()

        # 404：文档不存在
        r = await client.post("/api/admin/documents/999999/cancel", headers=admin_headers)
        assert r.status_code == 404
