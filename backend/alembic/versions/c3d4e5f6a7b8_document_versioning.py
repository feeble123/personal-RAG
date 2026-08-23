"""P0-8 版本化入库：document_versions/index_versions 表 + 版本指针 + chunks 挂版本

- 新增 document_versions、index_versions 两表
- documents.active_version_id / knowledge_bases.active_index_version_id 指针列
- legacy 回填：每个 document 插一条 active 版本，chunks 挂到对应版本
- chunks 唯一约束 (doc_id, chunk_index) → (document_version_id, chunk_index)
  （同文档多版本并存；SQLite 手动重建表，id 原样拷贝）

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-08-22
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.db.pg_guard import assert_sqlite_or_raise


# revision identifiers, used by Alembic.
revision: str = 'c3d4e5f6a7b8'
down_revision: Union[str, None] = 'b2c3d4e5f6a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_CHUNK_COLS = (
    "id, kb_id, doc_id, document_version_id, chunk_index, content, section, page, content_hash"
)


def upgrade() -> None:
    assert_sqlite_or_raise(op.get_bind().dialect.name)
    # 1) 版本表
    op.create_table(
        'document_versions',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('document_id', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('source_hash', sa.String(length=64), nullable=True),
        sa.Column('parser_profile', sa.JSON(), nullable=True),
        sa.Column('chunk_profile', sa.JSON(), nullable=True),
        sa.Column('quality_json', sa.JSON(), nullable=True),
        sa.Column('chunk_count', sa.Integer(), nullable=False),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.Column('activated_at', sa.DateTime(), nullable=True),
        sa.Column('retired_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['document_id'], ['documents.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_document_versions_document_id', 'document_versions', ['document_id'], unique=False)

    op.create_table(
        'index_versions',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('kb_id', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('physical_name', sa.String(length=100), nullable=False),
        sa.Column('expected_count', sa.Integer(), nullable=False),
        sa.Column('actual_count', sa.Integer(), nullable=False),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.Column('activated_at', sa.DateTime(), nullable=True),
        sa.Column('retired_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['kb_id'], ['knowledge_bases.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_index_versions_kb_id', 'index_versions', ['kb_id'], unique=False)

    # 2) 指针列（add column，无约束重建，batch 可行）
    with op.batch_alter_table('documents', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('active_version_id', sa.Integer(), nullable=True)
        )
    with op.batch_alter_table('knowledge_bases', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('active_index_version_id', sa.Integer(), nullable=True)
        )

    # 3) legacy 回填：每个 document 一条 active 版本（id 保真，引用不断）
    op.execute(
        """
        INSERT INTO document_versions
            (document_id, status, chunk_count, quality_json, created_at, activated_at)
        SELECT
            d.id, 'active', d.chunk_count, d.quality,
            COALESCE(d.created_at, CURRENT_TIMESTAMP),
            COALESCE(d.parsed_at, d.created_at)
        FROM documents d
        """
    )
    op.execute(
        """
        UPDATE documents
        SET active_version_id = (
            SELECT dv.id FROM document_versions dv
            WHERE dv.document_id = documents.id
        )
        """
    )
    # 已就绪的知识库回填一条 active 索引版本
    op.execute(
        """
        INSERT INTO index_versions (kb_id, status, physical_name, expected_count, actual_count)
        SELECT kb.id, 'active', 'kb_chunks',
               COALESCE(kb.chunk_count, 0), COALESCE(kb.chunk_count, 0)
        FROM knowledge_bases kb
        WHERE kb.status = 'ready'
        """
    )
    op.execute(
        """
        UPDATE knowledge_bases
        SET active_index_version_id = (
            SELECT iv.id FROM index_versions iv
            WHERE iv.kb_id = knowledge_bases.id
        )
        """
    )

    # 4) chunks 重建：挂 document_version_id + 唯一约束改 (document_version_id, chunk_index)
    op.execute(
        """
        CREATE TABLE chunks_new (
            id INTEGER NOT NULL PRIMARY KEY,
            kb_id INTEGER NOT NULL,
            doc_id INTEGER NOT NULL,
            document_version_id INTEGER NOT NULL,
            chunk_index INTEGER NOT NULL DEFAULT 0,
            content TEXT NOT NULL,
            section VARCHAR(300),
            page INTEGER,
            content_hash VARCHAR(64) NOT NULL,
            CONSTRAINT uq_chunks_ver_index UNIQUE (document_version_id, chunk_index),
            FOREIGN KEY(doc_id) REFERENCES documents (id) ON DELETE CASCADE,
            FOREIGN KEY(document_version_id) REFERENCES document_versions (id) ON DELETE CASCADE
        )
        """
    )
    op.execute(
        f"""
        INSERT INTO chunks_new ({_CHUNK_COLS})
        SELECT c.id, c.kb_id, c.doc_id, dv.id, c.chunk_index,
               c.content, c.section, c.page, c.content_hash
        FROM chunks c
        JOIN document_versions dv ON dv.document_id = c.doc_id
        """
    )
    op.execute("DROP TABLE chunks")
    op.execute("ALTER TABLE chunks_new RENAME TO chunks")
    op.create_index('ix_chunks_doc', 'chunks', ['doc_id'], unique=False)
    op.create_index('ix_chunks_doc_id', 'chunks', ['doc_id'], unique=False)
    op.create_index('ix_chunks_kb_id', 'chunks', ['kb_id'], unique=False)
    op.create_index('ix_chunks_document_version_id', 'chunks', ['document_version_id'], unique=False)


def downgrade() -> None:
    assert_sqlite_or_raise(op.get_bind().dialect.name)
    # 回滚到 P0-5 形态：chunks 无 document_version_id，唯一约束回 (doc_id, chunk_index)。
    # 注意：若某文档已有多个版本（重灌过），其 chunks 挂到不同版本会导致
    # (doc_id, chunk_index) 唯一冲突，此回滚会失败——重灌过的库不可回滚（同 P0-7/P0-5 惯例）。
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
    op.execute(
        """
        INSERT INTO chunks_new (id, kb_id, doc_id, chunk_index, content, section, page, content_hash)
        SELECT id, kb_id, doc_id, chunk_index, content, section, page, content_hash
        FROM chunks
        """
    )
    op.execute("DROP TABLE chunks")
    op.execute("ALTER TABLE chunks_new RENAME TO chunks")
    op.create_index('ix_chunks_doc', 'chunks', ['doc_id'], unique=False)
    op.create_index('ix_chunks_doc_id', 'chunks', ['doc_id'], unique=False)
    op.create_index('ix_chunks_kb_id', 'chunks', ['kb_id'], unique=False)

    with op.batch_alter_table('knowledge_bases', schema=None) as batch_op:
        batch_op.drop_column('active_index_version_id')
    with op.batch_alter_table('documents', schema=None) as batch_op:
        batch_op.drop_column('active_version_id')

    op.drop_index('ix_index_versions_kb_id', table_name='index_versions')
    op.drop_table('index_versions')
    op.drop_index('ix_document_versions_document_id', table_name='document_versions')
    op.drop_table('document_versions')
