"""本体关系的真相源表（ontology_relations）。

为什么加这张表：关系原来只能靠 Excel 台账导入，且只存在 Neo4j 里。
一旦重建图 / 换机器，人工整理的关系就没了。所以和前一张表同样的分工：
- PG（本表）= 真相源，管理端 CRUD 只写这里
- Neo4j     = 可重建的查询索引，由本表重放生成

唯一性：(profile_id, subject_uid, predicate, object_uid, kb_id)。

props / evidence 用 JSON：PG 侧升级为 JSONB，SQLite 保持 JSON。

Revision ID: j1a2b3c4d5e6
Revises: i9e5f6a7b8c9
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "j1a2b3c4d5e6"
down_revision: Union[str, None] = "i9e5f6a7b8c9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _is_pg() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    op.create_table(
        "ontology_relations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("profile_id", sa.String(length=50), nullable=False),
        sa.Column("subject_uid", sa.String(length=80), nullable=False),
        sa.Column("predicate", sa.String(length=60), nullable=False),
        sa.Column("object_uid", sa.String(length=80), nullable=False),
        sa.Column("kb_id", sa.Integer(), server_default="0", nullable=False),
        sa.Column("props", sa.JSON(), nullable=False),
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
            "profile_id",
            "subject_uid",
            "predicate",
            "object_uid",
            "kb_id",
            name="uq_ontology_relation_scope",
        ),
    )
    op.create_index(
        "ix_ontology_relations_profile_id", "ontology_relations", ["profile_id"], unique=False
    )
    op.create_index(
        "ix_ontology_relations_subject_uid", "ontology_relations", ["subject_uid"], unique=False
    )
    op.create_index(
        "ix_ontology_relations_object_uid", "ontology_relations", ["object_uid"], unique=False
    )
    op.create_index("ix_ontology_relations_kb_id", "ontology_relations", ["kb_id"], unique=False)
    op.create_index(
        "ix_ontology_relations_profile_pred",
        "ontology_relations",
        ["profile_id", "predicate"],
        unique=False,
    )

    # PG：JSON → JSONB（SQLite 无 JSONB，跳过）
    if _is_pg():
        for column in ("props", "evidence"):
            op.alter_column(
                "ontology_relations",
                column,
                type_=postgresql.JSONB(),
                existing_type=postgresql.JSON(),
                postgresql_using=f"{column}::jsonb",
                existing_nullable=column != "props",
            )


def downgrade() -> None:
    op.drop_index("ix_ontology_relations_profile_pred", table_name="ontology_relations")
    op.drop_index("ix_ontology_relations_kb_id", table_name="ontology_relations")
    op.drop_index("ix_ontology_relations_object_uid", table_name="ontology_relations")
    op.drop_index("ix_ontology_relations_subject_uid", table_name="ontology_relations")
    op.drop_index("ix_ontology_relations_profile_id", table_name="ontology_relations")
    op.drop_table("ontology_relations")
