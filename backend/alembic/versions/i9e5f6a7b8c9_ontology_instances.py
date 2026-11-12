"""本体实例真相源表（ontology_instances）。

为什么加这张表：参数先验这类本体知识**靠人维护**。如果只写 Neo4j，
重建图 / 换机器 / 清库一次，人工改过的值就没了。所以：
- PG（本表）= 真相源，管理端 CRUD 只写这里
- Neo4j     = 可重建的查询索引，由本表重放生成

唯一性：(profile_id, entity_type, name, kb_id)。
kb_id = 0 表示全局实例（跨库共享），local 类型才带真实 kb_id。

props / ranges / evidence 用 JSON：PG 侧升级为 JSONB，SQLite 保持 JSON。

Revision ID: i9e5f6a7b8c9
Revises: h8d4e5f6a7b8
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "i9e5f6a7b8c9"
down_revision: Union[str, None] = "h8d4e5f6a7b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _is_pg() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    op.create_table(
        "ontology_instances",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("uid", sa.String(length=80), nullable=False),
        sa.Column("profile_id", sa.String(length=50), nullable=False),
        sa.Column("entity_type", sa.String(length=40), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("kb_id", sa.Integer(), server_default="0", nullable=False),
        sa.Column("props", sa.JSON(), nullable=False),
        sa.Column("ranges", sa.JSON(), nullable=True),
        sa.Column("evidence", sa.JSON(), nullable=True),
        sa.Column("source", sa.String(length=20), server_default="manual", nullable=False),
        sa.Column("status", sa.String(length=20), server_default="active", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "profile_id", "entity_type", "name", "kb_id", name="uq_ontology_instance_scope"
        ),
    )
    op.create_index("ix_ontology_instances_uid", "ontology_instances", ["uid"], unique=False)
    op.create_index(
        "ix_ontology_instances_profile_id", "ontology_instances", ["profile_id"], unique=False
    )
    op.create_index(
        "ix_ontology_instances_entity_type", "ontology_instances", ["entity_type"], unique=False
    )
    op.create_index("ix_ontology_instances_kb_id", "ontology_instances", ["kb_id"], unique=False)
    op.create_index(
        "ix_ontology_instances_profile_type",
        "ontology_instances",
        ["profile_id", "entity_type"],
        unique=False,
    )

    # PG：JSON → JSONB（SQLite 无 JSONB，跳过）
    if _is_pg():
        for column in ("props", "ranges", "evidence"):
            op.alter_column(
                "ontology_instances",
                column,
                type_=postgresql.JSONB(),
                existing_type=postgresql.JSON(),
                postgresql_using=f"{column}::jsonb",
                existing_nullable=column != "props",
            )


def downgrade() -> None:
    op.drop_index("ix_ontology_instances_profile_type", table_name="ontology_instances")
    op.drop_index("ix_ontology_instances_kb_id", table_name="ontology_instances")
    op.drop_index("ix_ontology_instances_entity_type", table_name="ontology_instances")
    op.drop_index("ix_ontology_instances_profile_id", table_name="ontology_instances")
    op.drop_index("ix_ontology_instances_uid", table_name="ontology_instances")
    op.drop_table("ontology_instances")
