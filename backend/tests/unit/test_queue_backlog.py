"""单元 J 单元⑤：队列积压监控（指标 + 排队位置可解释）。

覆盖：
- queue_backlog_snapshot：各阶段在途数 + 最老等待时长（只含 count>0 阶段）
- queued_ahead_count：某 job 前面还有几个 queued 任务（先到先得）
- refresh_queue_metrics：Gauge set 正确（独立 registry 隔离）
- 上传 API 返回 queue_position 字段
"""
from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from prometheus_client import CollectorRegistry, generate_latest
from sqlalchemy import delete, select

from app.core.metrics import refresh_queue_metrics
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
        kb = KnowledgeBase(name=f"queue库{n}", status="ready")
        db.add(kb)
        await db.flush()
        doc = Document(
            kb_id=kb.id, filename=f"queue{n}.md", stored_path=f"queue{n}.md",
            file_type="md", status="pending",
        )
        db.add(doc)
        await db.commit()
        return kb.id, doc.id


async def _cleanup(kb_id: int) -> None:
    async with async_session_factory() as db:
        await db.execute(delete(KnowledgeBase).where(KnowledgeBase.id == kb_id))
        await db.commit()


async def _add_jobs(doc_id: int, stages: list[str]) -> list[int]:
    """按给定 stage 顺序建多个 job，返回 job_id 列表（queued 排前面）。"""
    ids: list[int] = []
    async with async_session_factory() as db:
        await db.execute(delete(IngestionJob).where(IngestionJob.document_id == doc_id))
        await db.commit()
        for s in stages:
            job = IngestionJob(document_id=doc_id, kind="ingest", stage=s)
            db.add(job)
            await db.flush()
            ids.append(job.id)
        await db.commit()
    return ids


class TestBacklogSnapshot:
    async def test_snapshot_counts_by_stage(self, client):
        """积压快照：按 stage 分组统计在途数。"""
        await _pause_worker()
        try:
            kb_id, doc_id = await _mk_doc()
            await _add_jobs(doc_id, ["queued", "queued", "parsing"])
            snap = await manager.queue_backlog_snapshot()
            by_stage = {s: c for s, c, _ in snap}
            assert by_stage["queued"] == 2, "应有 2 个 queued"
            assert by_stage["parsing"] == 1, "应有 1 个 parsing"
            await _cleanup(kb_id)
        finally:
            manager.start_worker()

    async def test_snapshot_oldest_wait(self, client):
        """积压快照：最老任务等待时长 > 0（created_at 为过去）。"""
        await _pause_worker()
        try:
            kb_id, doc_id = await _mk_doc()
            await _add_jobs(doc_id, ["queued"])
            async with async_session_factory() as db:
                job = (await db.execute(select(IngestionJob))).scalars().first()
                job.created_at = manager._now() - timedelta(seconds=30)
                await db.commit()
            snap = await manager.queue_backlog_snapshot()
            queued = next((c, w) for s, c, w in snap if s == "queued")
            assert queued[0] == 1
            assert queued[1] >= 29, "最老任务应等了约 30 秒"
            await _cleanup(kb_id)
        finally:
            manager.start_worker()

    async def test_snapshot_empty(self, client):
        """无活跃任务：快照为空列表。"""
        await _pause_worker()
        try:
            kb_id, doc_id = await _mk_doc()
            await _add_jobs(doc_id, ["succeeded"])  # 终态不计入
            snap = await manager.queue_backlog_snapshot()
            assert snap == [], "无活跃任务时快照应为空"
            await _cleanup(kb_id)
        finally:
            manager.start_worker()


class TestQueuedAhead:
    async def test_queued_ahead_count(self, client):
        """排队位置：某 queued job 前面有 2 个更早的 queued → ahead=2。"""
        await _pause_worker()
        try:
            kb_id, doc_id = await _mk_doc()
            ids = await _add_jobs(doc_id, ["queued", "queued", "queued"])
            ahead = await manager.queued_ahead_count(ids[2])  # 第 3 个 queued 前面有 2 个
            assert ahead == 2, "第三个 queued 前面应有 2 个"
            await _cleanup(kb_id)
        finally:
            manager.start_worker()

    async def test_queued_ahead_ignores_terminal(self, client):
        """排队位置：只数 id 更小的 queued，不数终态。"""
        await _pause_worker()
        try:
            kb_id, doc_id = await _mk_doc()
            ids = await _add_jobs(doc_id, ["succeeded", "queued"])
            ahead = await manager.queued_ahead_count(ids[1])  # queued 前面只有 succeeded（不计）
            assert ahead == 0, "终态不应计入排队位置"
            await _cleanup(kb_id)
        finally:
            manager.start_worker()


class TestRefreshQueueMetrics:
    async def test_refresh_sets_gauges(self):
        """refresh_queue_metrics：独立 registry 上 Gauge 值正确写入。"""
        reg = CollectorRegistry()
        refresh_queue_metrics([("queued", 3, 45.0), ("parsing", 1, 12.5)], registry=reg)
        out = generate_latest(reg).decode()
        assert 'rag_ingestion_queue_length{stage="queued"} 3.0' in out
        assert 'rag_ingestion_queue_length{stage="parsing"} 1.0' in out
        assert 'rag_ingestion_oldest_wait_seconds{stage="queued"} 45.0' in out


class TestUploadQueuePosition:
    async def test_upload_returns_queue_position(self, client, admin_headers):
        """上传 API 返回 queue_position 字段（新库首篇，前面 0 个排队）。"""
        r = await client.post("/api/admin/kbs", headers=admin_headers, json={"name": "积压验证库"})
        assert r.status_code == 201, r.text
        kb_id = r.json()["id"]
        try:
            md = "# 水利\n\n## 明渠\n\n明渠均匀流形成条件包括长直棱柱体渠道。\n\n".encode("utf-8")
            r = await client.post(
                f"/api/admin/kbs/{kb_id}/documents/upload",
                headers=admin_headers,
                files={"file": ("demo.md", md, "text/markdown")},
            )
            assert r.status_code == 201, r.text
            body = r.json()
            assert "queue_position" in body, "上传响应应含 queue_position 字段"
            assert isinstance(body["queue_position"], int)
        finally:
            await client.delete(f"/api/admin/kbs/{kb_id}", headers=admin_headers)
