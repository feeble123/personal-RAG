"""citation snapshot: chunk_id 可空 + ON DELETE SET NULL + 加索引

P0-5：重灌/删文档删除 chunk 时，历史引用行保留（快照字段 source/page/section/snippet/doc_id 可显示），
chunk_id 置 NULL 而非级联删行；chunk 仍存在的新引用正常回链。
SQLite 手动重建表；id 原样拷贝。

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-08-18
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'b2c3d4e5f6a7'
down_revision: Union[str, None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_COLS = "id, message_id, chunk_id, kb_id, doc_id, source, page, section, snippet, score, rank, created_at"


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE citations_new (
            id INTEGER NOT NULL PRIMARY KEY,
            message_id INTEGER NOT NULL,
            chunk_id INTEGER,
            kb_id INTEGER,
            doc_id INTEGER,
            source VARCHAR(255) NOT NULL,
            page INTEGER,
            section VARCHAR(300),
            snippet TEXT NOT NULL,
            score FLOAT,
            rank INTEGER,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT fk_citations_message FOREIGN KEY(message_id) REFERENCES messages (id) ON DELETE CASCADE,
            CONSTRAINT fk_citations_chunk FOREIGN KEY(chunk_id) REFERENCES chunks (id) ON DELETE SET NULL
        )
        """
    )
    op.execute(f"INSERT INTO citations_new ({_COLS}) SELECT {_COLS} FROM citations")
    op.execute("DROP TABLE citations")
    op.execute("ALTER TABLE citations_new RENAME TO citations")
    # 旧表索引随 DROP 消失，重建 + 新增 chunk_id 索引
    op.create_index('ix_citations_message', 'citations', ['message_id'], unique=False)
    op.create_index('ix_citations_message_id', 'citations', ['message_id'], unique=False)
    op.create_index('ix_citations_chunk_id', 'citations', ['chunk_id'], unique=False)


def downgrade() -> None:
    # 回滚到 baseline 形态：chunk_id NOT NULL + ON DELETE CASCADE。
    # 注意：若库内已有 chunk_id=NULL 的历史引用，回滚会失败（NOT NULL 约束）。
    op.execute(
        """
        CREATE TABLE citations_new (
            id INTEGER NOT NULL PRIMARY KEY,
            message_id INTEGER NOT NULL,
            chunk_id INTEGER NOT NULL,
            kb_id INTEGER,
            doc_id INTEGER,
            source VARCHAR(255) NOT NULL,
            page INTEGER,
            section VARCHAR(300),
            snippet TEXT NOT NULL,
            score FLOAT,
            rank INTEGER,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT fk_citations_message FOREIGN KEY(message_id) REFERENCES messages (id) ON DELETE CASCADE,
            CONSTRAINT fk_citations_chunk FOREIGN KEY(chunk_id) REFERENCES chunks (id) ON DELETE CASCADE
        )
        """
    )
    op.execute(f"INSERT INTO citations_new ({_COLS}) SELECT {_COLS} FROM citations")
    op.execute("DROP TABLE citations")
    op.execute("ALTER TABLE citations_new RENAME TO citations")
    op.create_index('ix_citations_message', 'citations', ['message_id'], unique=False)
    op.create_index('ix_citations_message_id', 'citations', ['message_id'], unique=False)
