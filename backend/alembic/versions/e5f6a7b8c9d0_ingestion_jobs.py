"""P0-9 持久化入库任务：ingestion_jobs 表 + 遗留 parsing/embedding 任务恢复

- 新增 ingestion_jobs 表（DB 是任务真相源：进程重启可恢复、失败可定位 stage）
- 遗留状态迁移：当前 status IN ('parsing','embedding') 的文档（进程中断的幽灵任务）
  → 插入一条 failed job + 文档标 failed，启动后不再有永远卡住的任务

Revision ID: e5f6a7b8c9d0
Revises: d6e7f8a9b0c1
Create Date: 2026-08-22
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e5f6a7b8c9d0'
down_revision: Union[str, None] = 'd6e7f8a9b0c1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'ingestion_jobs',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('document_id', sa.Integer(), nullable=False),
        sa.Column('kind', sa.String(length=10), nullable=False),
        sa.Column('stage', sa.String(length=20), nullable=False),
        sa.Column('attempt', sa.Integer(), nullable=False),
        sa.Column('lease_owner', sa.String(length=50), nullable=True),
        sa.Column('lease_until', sa.DateTime(), nullable=True),
        sa.Column('heartbeat_at', sa.DateTime(), nullable=True),
        sa.Column('progress', sa.JSON(), nullable=True),
        sa.Column('error_code', sa.String(length=50), nullable=True),
        sa.Column('error_detail', sa.Text(), nullable=True),
        sa.Column('cancel_requested', sa.Boolean(), nullable=False, server_default=sa.text('0')),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.ForeignKeyConstraint(['document_id'], ['documents.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_ingestion_jobs_document_id', 'ingestion_jobs', ['document_id'], unique=False)

    # 遗留任务恢复：把进程中断残留的 parsing/embedding 幽灵文档标 failed + 记 job
    op.execute(
        """
        INSERT INTO ingestion_jobs
            (document_id, kind, stage, attempt, error_code, error_detail)
        SELECT
            d.id, 'ingest', 'failed', 0,
            'PROCESS_INTERRUPTED',
            '进程中断，任务未完成（启动恢复时标记失败）'
        FROM documents d
        WHERE d.status IN ('parsing', 'embedding')
        """
    )
    op.execute(
        """
        UPDATE documents
        SET status = 'failed',
            error_message = '进程中断，任务未完成（启动恢复时标记失败）'
        WHERE status IN ('parsing', 'embedding')
        """
    )


def downgrade() -> None:
    op.drop_index('ix_ingestion_jobs_document_id', table_name='ingestion_jobs')
    op.drop_table('ingestion_jobs')
