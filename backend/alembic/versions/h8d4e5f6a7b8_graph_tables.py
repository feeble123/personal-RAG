"""第一期知识图谱：新增三张 PG 表（运行记录 / 边证据 / 实体别名）。

设计：PG 是真相源，Neo4j 只存节点与关系（可重建）。三张表分别解决：
- graph_sync_runs      ：跑没跑成功、抽了多少、哪些行被拒（可审计可重试）
- graph_edge_evidence  ：每条事实的原文出处（chunk_id/页码/章节/条款），答案可点回原文
- graph_entity_aliases ：问题里的词 → 图节点（含别名），查询侧链接不依赖图库

warnings 用 JSON：PG 侧升级为 JSONB（与 G5-1 一致），SQLite 保持 JSON。

Revision ID: h8d4e5f6a7b8
Revises: g7c3d4e5f6a7
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "h8d4e5f6a7b8"
down_revision: Union[str, None] = "g7c3d4e5f6a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _is_pg() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    op.create_table(
        "graph_sync_runs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("kb_id", sa.Integer(), nullable=False),
        sa.Column("doc_id", sa.Integer(), nullable=True),
        sa.Column("version_id", sa.Integer(), nullable=True),
        sa.Column("mode", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("entity_count", sa.Integer(), nullable=False),
        sa.Column("relation_count", sa.Integer(), nullable=False),
        sa.Column("warnings", sa.JSON(), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column(
            "started_at", sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_graph_sync_runs_kb_id", "graph_sync_runs", ["kb_id"], unique=False)
    op.create_index("ix_graph_sync_runs_doc_id", "graph_sync_runs", ["doc_id"], unique=False)
    op.create_index("ix_graph_sync_runs_kb_doc", "graph_sync_runs", ["kb_id", "doc_id"], unique=False)

    op.create_table(
        "graph_edge_evidence",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("edge_key", sa.String(length=400), nullable=False),
        sa.Column("kb_id", sa.Integer(), nullable=False),
        sa.Column("doc_id", sa.Integer(), nullable=True),
        sa.Column("version_id", sa.Integer(), nullable=True),
        sa.Column("chunk_id", sa.Integer(), nullable=True),
        sa.Column("doc_name", sa.String(length=255), nullable=False),
        sa.Column("page", sa.Integer(), nullable=True),
        sa.Column("section", sa.String(length=300), nullable=True),
        sa.Column("clause_no", sa.String(length=50), nullable=True),
        sa.Column("snippet", sa.Text(), nullable=False),
        sa.Column("extractor", sa.String(length=20), nullable=False),
        sa.Column("rule_id", sa.String(length=50), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_graph_edge_evidence_edge_key", "graph_edge_evidence", ["edge_key"], unique=False)
    op.create_index("ix_graph_edge_evidence_kb_id", "graph_edge_evidence", ["kb_id"], unique=False)
    op.create_index("ix_graph_edge_evidence_doc_id", "graph_edge_evidence", ["doc_id"], unique=False)
    op.create_index("ix_graph_edge_evidence_chunk_id", "graph_edge_evidence", ["chunk_id"], unique=False)
    op.create_index("ix_graph_edge_evidence_run_id", "graph_edge_evidence", ["run_id"], unique=False)
    op.create_index(
        "ix_graph_evidence_version", "graph_edge_evidence", ["kb_id", "version_id"], unique=False
    )

    op.create_table(
        "graph_entity_aliases",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("node_uid", sa.String(length=80), nullable=False),
        sa.Column("entity_type", sa.String(length=40), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("alias", sa.String(length=200), nullable=False),
        sa.Column("normalized_alias", sa.String(length=200), nullable=False),
        sa.Column("kb_id", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "normalized_alias", "kb_id", "node_uid", name="uq_graph_alias_normalized_kb_uid"
        ),
    )
    op.create_index("ix_graph_entity_aliases_node_uid", "graph_entity_aliases", ["node_uid"], unique=False)
    op.create_index(
        "ix_graph_entity_aliases_normalized_alias", "graph_entity_aliases", ["normalized_alias"], unique=False
    )
    op.create_index("ix_graph_entity_aliases_kb_id", "graph_entity_aliases", ["kb_id"], unique=False)

    # PG：warnings JSON → JSONB（SQLite 无 JSONB，跳过）
    if _is_pg():
        op.alter_column(
            "graph_sync_runs",
            "warnings",
            type_=postgresql.JSONB(),
            existing_type=postgresql.JSON(),
            postgresql_using="warnings::jsonb",
            existing_nullable=True,
        )


def downgrade() -> None:
    op.drop_index("ix_graph_entity_aliases_kb_id", table_name="graph_entity_aliases")
    op.drop_index("ix_graph_entity_aliases_normalized_alias", table_name="graph_entity_aliases")
    op.drop_index("ix_graph_entity_aliases_node_uid", table_name="graph_entity_aliases")
    op.drop_table("graph_entity_aliases")

    op.drop_index("ix_graph_evidence_version", table_name="graph_edge_evidence")
    op.drop_index("ix_graph_edge_evidence_run_id", table_name="graph_edge_evidence")
    op.drop_index("ix_graph_edge_evidence_chunk_id", table_name="graph_edge_evidence")
    op.drop_index("ix_graph_edge_evidence_doc_id", table_name="graph_edge_evidence")
    op.drop_index("ix_graph_edge_evidence_kb_id", table_name="graph_edge_evidence")
    op.drop_index("ix_graph_edge_evidence_edge_key", table_name="graph_edge_evidence")
    op.drop_table("graph_edge_evidence")

    op.drop_index("ix_graph_sync_runs_kb_doc", table_name="graph_sync_runs")
    op.drop_index("ix_graph_sync_runs_doc_id", table_name="graph_sync_runs")
    op.drop_index("ix_graph_sync_runs_kb_id", table_name="graph_sync_runs")
    op.drop_table("graph_sync_runs")
