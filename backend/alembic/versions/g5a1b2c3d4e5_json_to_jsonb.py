"""G5-1：JSON 列 → JSONB（PG 方言下）。

- 6 个 JSON 列：documents.quality / chunks.table_data /
  document_versions.parser_profile / chunk_profile / quality_json /
  ingestion_jobs.progress
- 收益：JSONB 支持 GIN 索引 + `->>` 运算符查询（未来检索出处/质量分析用）
- SQLite 无 JSONB 类型 → 跳过（模型层 with_variant 已保证 SQLite 仍编译成 JSON）

Revision ID: g5a1b2c3d4e5
Revises: 3c4d5e6f7a8b
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'g5a1b2c3d4e5'
down_revision: Union[str, None] = '3c4d5e6f7a8b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (表, 列) 顺序：JSON → JSONB
_JSON_COLUMNS: tuple[tuple[str, str], ...] = (
    ("documents", "quality"),
    ("chunks", "table_data"),
    ("document_versions", "parser_profile"),
    ("document_versions", "chunk_profile"),
    ("document_versions", "quality_json"),
    ("ingestion_jobs", "progress"),
)


def _is_postgres() -> bool:
    """迁移连接的方言是否为 PG（SQLite 无 JSONB，跳过）。"""
    bind = op.get_bind()
    return bind.dialect.name == "postgresql"


def upgrade() -> None:
    if not _is_postgres():
        return
    for table, col in _JSON_COLUMNS:
        op.alter_column(
            table,
            col,
            type_=postgresql.JSONB(),
            existing_type=postgresql.JSON(),
            existing_nullable=True,
            postgresql_using=f"{col}::jsonb",
        )


def downgrade() -> None:
    if not _is_postgres():
        return
    for table, col in _JSON_COLUMNS:
        op.alter_column(
            table,
            col,
            type_=postgresql.JSON(),
            existing_type=postgresql.JSONB(),
            existing_nullable=True,
            postgresql_using=f"{col}::json",
        )
