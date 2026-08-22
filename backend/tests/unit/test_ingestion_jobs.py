"""P0-9 持久化入库任务：job 表结构 + 遗留任务恢复 + 幂等去重（单元1）。

- ingestion_jobs 表经 alembic 迁移创建（test_alembic_baseline 已覆盖 compare_metadata）
- 遗留：parsing/embedding 幽灵文档升级后标 failed + 记录 job
- 幂等：同一文档重复 enqueue 只产生一个活跃 job
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import delete, func, select

from app.db.models import Document, IngestionJob, KnowledgeBase
from app.db.session import async_session_factory
from app.modules.ingestion import manager

pytestmark = pytest.mark.asyncio

_cnt = {"n": 0}


async def _mk_doc(status: str = "pending") -> tuple[int, int]:
    """建一个库 + 一个文档，返回 (kb_id, doc_id)。"""
    _cnt["n"] += 1
    n = _cnt["n"]
    async with async_session_factory() as db:
        kb = KnowledgeBase(name=f"job库{n}", status="ready")
        db.add(kb)
        await db.flush()
        doc = Document(
            kb_id=kb.id, filename=f"job{n}.md", stored_path=f"job{n}.md",
            file_type="md", status=status,
        )
        db.add(doc)
        await db.commit()
        return kb.id, doc.id


async def _cleanup(kb_id: int) -> None:
    async with async_session_factory() as db:
        await db.execute(delete(KnowledgeBase).where(KnowledgeBase.id == kb_id))
        await db.commit()


class TestJobModel:
    async def test_ingestion_job_table_exists(self, client, admin_headers, sample_kb):
        """ingestion_jobs 表可读写（session 建表后表存在）。"""
        kb_id, doc_id = sample_kb
        # 停 worker：避免它把手动插入的 queued job 领走改状态，破坏本测试断言
        manager.stop_worker()
        try:
            await asyncio.sleep(0.05)
            async with async_session_factory() as db:
                await db.execute(delete(IngestionJob))
                await db.commit()
                db.add(IngestionJob(document_id=doc_id, kind="ingest", stage="queued"))
                await db.commit()
            async with async_session_factory() as db:
                jobs = (await db.execute(select(IngestionJob))).scalars().all()
                assert jobs, "ingestion_jobs 应可写入"
                assert jobs[0].stage == "queued"
                assert jobs[0].cancel_requested is False
                assert jobs[0].attempt == 0
        finally:
            manager.start_worker()


class TestLegacyRecovery:
    async def test_migration_recovers_stuck_docs(self, tmp_path, monkeypatch):
        """遗留 parsing/embedding 文档升级后标 failed + 记 job（模拟迁移）。"""
        from alembic import command
        from alembic.config import Config
        from pathlib import Path

        import sqlite3

        BASE_DIR = Path(__file__).resolve().parents[2]

        # 建一个"旧库"（已到 d6e7f8a9b0c1，含 parsing/embedding 幽灵文档），再 upgrade head
        db_file = tmp_path / "legacy.db"
        monkeypatch.setenv("ALEMBIC_DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")

        cfg = Config(str(BASE_DIR / "alembic.ini"))
        # 用 run_sync 跑 alembic，避免与事件循环冲突
        from sqlalchemy import create_engine

        def _upgrade(rev: str):
            command.upgrade(cfg, rev)

        await asyncio.to_thread(_upgrade, "d6e7f8a9b0c1")

        # 手动插入幽灵文档 + 需要的父表（documents 依赖 knowledge_bases）
        con = sqlite3.connect(str(db_file))
        con.execute("INSERT INTO knowledge_bases (name, doc_count, chunk_count, status, answer_style) VALUES ('幽灵库', 1, 0, 'ready', 'standard')")
        kb_id = con.execute("SELECT id FROM knowledge_bases WHERE name='幽灵库'").fetchone()[0]
        con.execute(
            "INSERT INTO documents (kb_id, filename, stored_path, file_type, file_size, status, chunk_count, chunk_strategy, doc_type) "
            f"VALUES ({kb_id}, 'ghost.md', 'ghost.md', 'md', 0, 'parsing', 0, 'old', 'other')"
        )
        con.commit()
        con.close()

        # upgrade head（应触发遗留恢复）
        await asyncio.to_thread(_upgrade, "head")

        con = sqlite3.connect(str(db_file))
        doc_status = con.execute("SELECT status FROM documents WHERE filename='ghost.md'").fetchone()[0]
        job_rows = con.execute("SELECT stage, error_code FROM ingestion_jobs").fetchall()
        con.close()

        assert doc_status == "failed", f"幽灵文档应标 failed, 实得 {doc_status}"
        assert any(stage == "failed" for stage, _ in job_rows), f"应有 failed job, 实得 {job_rows}"


class TestEnqueueIdempotent:
    async def test_enqueue_creates_one_active_job(self, client, admin_headers, sample_kb):
        """重复 enqueue 同一文档只产生一个活跃 job（单元2：业务层去重生效）。"""
        _, doc_id = sample_kb
        # 停 worker：避免它抢先领走 queued job 处理（改变终态）导致幂等断言失效
        manager.stop_worker()
        try:
            await asyncio.sleep(0.05)
            # 清掉 sample_kb 已有任务（已 succeeded/failed），手动验证幂等
            async with async_session_factory() as db:
                await db.execute(delete(IngestionJob))
                await db.commit()

            from app.modules.ingestion import manager as mgr
            # 真实写 job 路径：连续两次 enqueue_ingestion_async → 第二次跳过
            job1 = await mgr.enqueue_ingestion_async(doc_id, kind="ingest")
            job2 = await mgr.enqueue_ingestion_async(doc_id, kind="ingest")
            assert job1 is not None, "第一次应创建 job"
            assert job2 is None, "已有活跃 job 时第二次应跳过（不重复投递）"

            async with async_session_factory() as db:
                jobs = (await db.execute(
                    select(IngestionJob).where(IngestionJob.document_id == doc_id)
                )).scalars().all()
                assert len(jobs) == 1, f"应只有 1 个 job, 实得 {len(jobs)}"
                assert jobs[0].stage == "queued"
        finally:
            manager.start_worker()
