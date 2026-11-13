"""自动抽取的待审提案表（ontology_proposals，F4）。

为什么不直接写进实例：机器抽出来的值和人工查证过的值混在一起，事后分不清
哪条是查过的、哪条是猜的。暂存区让「机器猜的」先列出来，人点接受才写实例。

dedupe_key = sha1(profile|instance|attribute|value)，同一结论反复跑不重复堆积。

Revision ID: k2b3c4d5e6f7
Revises: j1a2b3c4d5e6
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "k2b3c4d5e6f7"
down_revision: Union[str, None] = "j1a2b3c4d5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _is_pg() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _json():
    return postgresql.JSONB(astext_type=sa.Text()) if _is_pg() else sa.JSON()


def upgrade() -> None:
    op.create_table(
        "ontology_proposals",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("profile_id", sa.String(length=50), nullable=False),
        sa.Column("instance_uid", sa.String(length=80), nullable=False),
        sa.Column("entity_type", sa.String(length=60), nullable=False),
        sa.Column("attribute", sa.String(length=60), nullable=False),
        sa.Column("value", _json(), nullable=False),
        sa.Column("method", sa.String(length=10), server_default="rule", nullable=False),
        sa.Column("confidence", sa.Float(), server_default="0", nullable=False),
        sa.Column("evidence", _json(), nullable=True),
        sa.Column("dedupe_key", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("profile_id", "dedupe_key", name="uq_ontology_proposal_dedupe"),
    )
    op.create_index("ix_ontology_proposals_profile_id", "ontology_proposals", ["profile_id"])
    op.create_index("ix_ontology_proposals_instance_uid", "ontology_proposals", ["instance_uid"])
    op.create_index("ix_ontology_proposals_status", "ontology_proposals", ["status"])
    op.create_index(
        "ix_ontology_proposals_profile_status", "ontology_proposals", ["profile_id", "status"]
    )


def downgrade() -> None:
    op.drop_index("ix_ontology_proposals_profile_status", table_name="ontology_proposals")
    op.drop_index("ix_ontology_proposals_status", table_name="ontology_proposals")
    op.drop_index("ix_ontology_proposals_instance_uid", table_name="ontology_proposals")
    op.drop_index("ix_ontology_proposals_profile_id", table_name="ontology_proposals")
    op.drop_table("ontology_proposals")
