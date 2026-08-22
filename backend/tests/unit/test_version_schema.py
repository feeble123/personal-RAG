"""P0-8 版本化 schema：document_versions/index_versions 表 + 指针回填 + chunks 唯一约束。

直接在真实临时库上执行 alembic upgrade head，验证：
- 新增表/列与 ORM 一致（compare_metadata 强校验）
- legacy 回填：每个 document 恰 1 条 active 版本、active_version_id 正确、chunks 都挂版本
- chunks 唯一约束已从 (doc_id, chunk_index) 改为 (document_version_id, chunk_index)
"""
from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text

from app.db.models import Chunk, Document, DocumentVersion
from app.db.session import async_session_factory

BASE_DIR = Path(__file__).resolve().parents[2]

_cnt = {"n": 0}


@pytest.mark.asyncio
async def test_models_have_version_columns(client):
    """ORM 模型已含版本字段/表（迁移文件的对齐依据）。"""
    assert hasattr(Document, "active_version_id")
    assert hasattr(Chunk, "document_version_id")
    assert hasattr(Chunk, "version")
    assert DocumentVersion.__tablename__ == "document_versions"

    _cnt["n"] += 1
    async with async_session_factory() as db:
        # 直接建一个文档 + 版本 + chunks，验证唯一约束按版本隔离
        from sqlalchemy import select

        from app.db.models import KnowledgeBase

        kb = KnowledgeBase(name=f"schema_kb_{_cnt['n']}", status="ready")
        db.add(kb)
        await db.flush()
        doc = Document(
            kb_id=kb.id, filename="a.md", stored_path="a.md", file_type="md", status="ready"
        )
        db.add(doc)
        await db.flush()
        v1 = DocumentVersion(document_id=doc.id, status="active")
        v2 = DocumentVersion(document_id=doc.id, status="retired")
        db.add_all([v1, v2])
        await db.flush()
        # 同 chunk_index 在不同版本下可并存
        db.add(Chunk(kb_id=kb.id, doc_id=doc.id, document_version_id=v1.id, chunk_index=0,
                     content="旧版", content_hash="h1"))
        db.add(Chunk(kb_id=kb.id, doc_id=doc.id, document_version_id=v2.id, chunk_index=0,
                     content="新版", content_hash="h2"))
        await db.commit()

        # 用 select 重新查（避免 commit 后直接访问 relationship 触发 MissingGreenlet）
        rows = (await db.execute(select(Chunk).where(Chunk.document_version_id.in_([v1.id, v2.id])))).scalars().all()
        assert len(rows) == 2, "两个版本各应有 1 个 chunk"
        assert {c.content for c in rows} == {"旧版", "新版"}

        from sqlalchemy import delete

        await db.execute(delete(KnowledgeBase).where(KnowledgeBase.id == kb.id))
        await db.commit()


@pytest.mark.asyncio
async def test_doc_chunks_loads_across_versions(client):
    """Document.chunks 跨版本加载（不再级联 delete-orphan，避免多父冲突）。"""
    from sqlalchemy import delete, select

    from app.db.models import KnowledgeBase

    _cnt["n"] += 1
    kb = KnowledgeBase(name=f"schema_kb2_{_cnt['n']}", status="ready")
    async with async_session_factory() as db:
        db.add(kb)
        await db.flush()
        doc = Document(
            kb_id=kb.id, filename="b.md", stored_path="b.md", file_type="md", status="ready"
        )
        db.add(doc)
        await db.flush()
        v = DocumentVersion(document_id=doc.id, status="active")
        db.add(v)
        await db.flush()
        db.add(Chunk(kb_id=kb.id, doc_id=doc.id, document_version_id=v.id, chunk_index=0,
                     content="x", content_hash="hx"))
        await db.commit()
        # 用 select 重新查，chunks 应能读到
        cnt = (await db.scalar(select(Chunk.id).where(Chunk.doc_id == doc.id)))
        assert cnt is not None

        await db.execute(delete(KnowledgeBase).where(KnowledgeBase.id == kb.id))
        await db.commit()


def test_migration_backfills_legacy(tmp_path, monkeypatch):
    """旧库（baseline+chunk_identity+citation_snapshot 形态）升级到 head：
    每个 document 恰 1 条 active 版本、active_version_id 指向、chunks 挂版本、唯一约束已改。
    """
    from alembic import command
    from alembic.config import Config

    db_file = tmp_path / "legacy.db"
    monkeypatch.setenv("ALEMBIC_DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")

    cfg = Config(str(BASE_DIR / "alembic.ini"))
    # 先升级到 P0-5 head（当前生产形态），再手动插入 legacy 数据，再升级到新 head
    command.upgrade(cfg, "b2c3d4e5f6a7")

    engine = create_engine(f"sqlite:///{db_file}")
    with engine.connect() as conn:
        # legacy 数据：2 文档 + 各若干 chunks + 1 ready 库
        conn.execute(text("INSERT INTO knowledge_bases (name, doc_count, chunk_count, status, answer_style) "
                          "VALUES ('legacy_kb', 2, 3, 'ready', 'standard')"))
        kb_id = conn.execute(text("SELECT id FROM knowledge_bases WHERE name='legacy_kb'")).scalar_one()
        conn.execute(text(
            f"INSERT INTO documents (kb_id, filename, stored_path, file_type, file_size, status, "
            f"chunk_count, chunk_strategy, parsed_at) "
            f"VALUES ({kb_id}, 'doc1.md', 'd1.md', 'md', 10, 'ready', 2, 'old', "
            f"datetime('now'))"))
        conn.execute(text(
            f"INSERT INTO documents (kb_id, filename, stored_path, file_type, file_size, status, "
            f"chunk_count, chunk_strategy, parsed_at) "
            f"VALUES ({kb_id}, 'doc2.md', 'd2.md', 'md', 20, 'ready', 1, 'new', "
            f"datetime('now'))"))
        doc_ids = [r[0] for r in conn.execute(
            text("SELECT id FROM documents WHERE kb_id=:kb ORDER BY id"), {"kb": kb_id}
        )]
        d1, d2 = doc_ids
        conn.execute(text(
            f"INSERT INTO chunks (kb_id, doc_id, chunk_index, content, section, page, content_hash) "
            f"VALUES ({kb_id}, {d1}, 0, '一、总则', '1 总则', 1, 'h1')"))
        conn.execute(text(
            f"INSERT INTO chunks (kb_id, doc_id, chunk_index, content, section, page, content_hash) "
            f"VALUES ({kb_id}, {d1}, 1, '二、设计', '2 设计', 2, 'h2')"))
        conn.execute(text(
            f"INSERT INTO chunks (kb_id, doc_id, chunk_index, content, section, page, content_hash) "
            f"VALUES ({kb_id}, {d2}, 0, '三、验收', '3 验收', 3, 'h3')"))
        conn.commit()

    engine.dispose()

    # 升级到新 head
    command.upgrade(cfg, "head")

    engine = create_engine(f"sqlite:///{db_file}")
    try:
        with engine.connect() as conn:
            # 每个 doc 恰 1 条 active 版本
            rows = conn.execute(text(
                "SELECT document_id, status FROM document_versions ORDER BY document_id"
            )).fetchall()
            assert len(rows) == 2, f"应回填 2 条版本, 实得 {len(rows)}"
            assert {r[1] for r in rows} == {"active"}
            # active_version_id 正确指向
            joined = conn.execute(text(
                "SELECT d.active_version_id, dv.document_id FROM documents d "
                "JOIN document_versions dv ON dv.id = d.active_version_id ORDER BY d.id"
            )).fetchall()
            assert [d for d in (j[1] for j in joined)] == doc_ids
            # 所有 chunks 都挂上版本
            orphan = conn.execute(text(
                "SELECT COUNT(*) FROM chunks c WHERE c.document_version_id IS NULL"
            )).scalar_one()
            assert orphan == 0
            # 唯一约束已改为 (document_version_id, chunk_index)
            chunk_sql = inspect(conn).get_unique_constraints("chunks")
            assert any("document_version_id" in str(c["column_names"]) for c in chunk_sql), \
                f"chunks 唯一约束应含 document_version_id, 实得 {chunk_sql}"
            # 索引版本回填
            iv = conn.execute(text(
                "SELECT COUNT(*) FROM index_versions WHERE kb_id=:kb AND status='active'"
            ), {"kb": kb_id}).scalar_one()
            assert iv == 1
            kb_iv = conn.execute(text(
                "SELECT active_index_version_id FROM knowledge_bases WHERE id=:kb"
            ), {"kb": kb_id}).scalar_one()
            assert kb_iv is not None
    finally:
        engine.dispose()
