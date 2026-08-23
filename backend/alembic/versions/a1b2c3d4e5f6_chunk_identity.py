"""chunk identity: content_hash 去全局唯一，加 (doc_id, chunk_index) 复合唯一

P0-7：同内容跨文档保留独立 chunk（来源正确、互不牵连）；embedding 仍按 content_hash 复用缓存。
SQLite 手动重建表（确定性强，不受旧库双份唯一约束命名差异影响）；id 原样拷贝，引用不丢。

Revision ID: a1b2c3d4e5f6
Revises: f6a1e2bb1fd9
Create Date: 2026-08-18
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

from app.db.pg_guard import assert_sqlite_or_raise


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = 'f6a1e2bb1fd9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_COLS = "id, kb_id, doc_id, chunk_index, content, section, page, content_hash"


def upgrade() -> None:
    assert_sqlite_or_raise(op.get_bind().dialect.name)
    op.execute(
        """
        CREATE TABLE chunks_new (
            id INTEGER NOT NULL PRIMARY KEY,
            kb_id INTEGER NOT NULL,
            doc_id INTEGER NOT NULL,
            chunk_index INTEGER NOT NULL DEFAULT 0,
            content TEXT NOT NULL,
            section VARCHAR(300),
            page INTEGER,
            content_hash VARCHAR(64) NOT NULL,
            CONSTRAINT uq_chunks_doc_index UNIQUE (doc_id, chunk_index),
            FOREIGN KEY(doc_id) REFERENCES documents (id) ON DELETE CASCADE
        )
        """
    )
    op.execute(f"INSERT INTO chunks_new ({_COLS}) SELECT {_COLS} FROM chunks")
    op.execute("DROP TABLE chunks")
    op.execute("ALTER TABLE chunks_new RENAME TO chunks")
    # 旧表索引随 DROP 消失，重建
    op.create_index('ix_chunks_doc', 'chunks', ['doc_id'], unique=False)
    op.create_index('ix_chunks_doc_id', 'chunks', ['doc_id'], unique=False)
    op.create_index('ix_chunks_kb_id', 'chunks', ['kb_id'], unique=False)


def downgrade() -> None:
    assert_sqlite_or_raise(op.get_bind().dialect.name)
    # 回滚到 baseline 形态：content_hash 列级唯一，无 (doc_id, chunk_index) 唯一。
    # 注意：若库内已出现跨文档重复 content_hash，此回滚会因唯一冲突失败（P0-7 后重灌过的库不可回滚）。
    op.execute(
        """
        CREATE TABLE chunks_new (
            id INTEGER NOT NULL PRIMARY KEY,
            kb_id INTEGER NOT NULL,
            doc_id INTEGER NOT NULL,
            chunk_index INTEGER NOT NULL DEFAULT 0,
            content TEXT NOT NULL,
            section VARCHAR(300),
            page INTEGER,
            content_hash VARCHAR(64) NOT NULL,
            UNIQUE (content_hash),
            FOREIGN KEY(doc_id) REFERENCES documents (id) ON DELETE CASCADE
        )
        """
    )
    op.execute(f"INSERT INTO chunks_new ({_COLS}) SELECT {_COLS} FROM chunks")
    op.execute("DROP TABLE chunks")
    op.execute("ALTER TABLE chunks_new RENAME TO chunks")
    op.create_index('ix_chunks_doc', 'chunks', ['doc_id'], unique=False)
    op.create_index('ix_chunks_doc_id', 'chunks', ['doc_id'], unique=False)
    op.create_index('ix_chunks_kb_id', 'chunks', ['kb_id'], unique=False)
