"""G5-2：时间列统一 UTC tz-aware（PG 下 TIMESTAMP → timestamptz）。

- 全部 32 个 DateTime 列 → DateTime(timezone=True)
- PG：ALTER COLUMN TYPE timestamptz（默认按 session timezone 解释 naive 值）
  - created_at/updated_at 等 server_default=func.now() 生成的本就是北京时间墙钟 → 正确
  - parsed_at 历史被 Python 写成 UTC 却按 naive 存 → 迁移后显示偏 8 小时（如实保留，
    仅用于展示解析时间，不纠正）
- SQLite：无时区概念（DATETIME 无 timezone 属性），跳过

Revision ID: g5b2c3d4e5f6
Revises: g5a1b2c3d4e5
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'g5b2c3d4e5f6'
down_revision: Union[str, None] = 'g5a1b2c3d4e5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (表, 列) 顺序：TIMESTAMP WITHOUT TIME ZONE → timestamptz
_TIMESTAMP_COLUMNS: tuple[tuple[str, str], ...] = (
    ("users", "created_at"),
    ("users", "updated_at"),
    ("auth_sessions", "expires_at"),
    ("auth_sessions", "revoked_at"),
    ("auth_sessions", "last_used_at"),
    ("auth_sessions", "created_at"),
    ("conversations", "last_message_at"),
    ("conversations", "created_at"),
    ("conversations", "updated_at"),
    ("messages", "created_at"),
    ("knowledge_bases", "created_at"),
    ("knowledge_bases", "updated_at"),
    ("documents", "created_at"),
    ("documents", "parsed_at"),
    ("document_versions", "created_at"),
    ("document_versions", "activated_at"),
    ("document_versions", "retired_at"),
    ("ingestion_jobs", "lease_until"),
    ("ingestion_jobs", "heartbeat_at"),
    ("ingestion_jobs", "created_at"),
    ("ingestion_jobs", "updated_at"),
    ("index_versions", "created_at"),
    ("index_versions", "activated_at"),
    ("index_versions", "retired_at"),
    ("citations", "created_at"),
    ("embedding_cache", "created_at"),
    ("semantic_cache", "created_at"),
    ("semantic_cache", "updated_at"),
    ("qa_memory", "created_at"),
    ("qa_memory", "updated_at"),
    ("audit_logs", "created_at"),
)


def _is_postgres() -> bool:
    bind = op.get_bind()
    return bind.dialect.name == "postgresql"


def upgrade() -> None:
    if not _is_postgres():
        return
    for table, col in _TIMESTAMP_COLUMNS:
        op.alter_column(
            table,
            col,
            type_=postgresql.TIMESTAMP(timezone=True),
            existing_type=postgresql.TIMESTAMP(timezone=False),
            existing_nullable=True,
            postgresql_using=f"{col}::timestamptz",
        )


def downgrade() -> None:
    if not _is_postgres():
        return
    for table, col in _TIMESTAMP_COLUMNS:
        op.alter_column(
            table,
            col,
            type_=postgresql.TIMESTAMP(timezone=False),
            existing_type=postgresql.TIMESTAMP(timezone=True),
            existing_nullable=True,
            postgresql_using=f"{col}::timestamp",
        )
