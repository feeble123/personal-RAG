"""单元 2：知识库增加业务域（knowledge_bases.business_domain）。

设计：业务域挂在**知识库**上，一个库一个域，文档继承所在库的域。
取值见 app/core/taxonomy.py（6 个域），旧数据一律先归到 general（综合与基础），
再由 scripts/backfill_classification.py 按人工确认的映射表逐库归类。

- 列带 server_default='general'，PG/SQLite 都能加，历史行为 NULL → 不需要
- 加索引：列表页与检索按域过滤
- downgrade 直接删列（无数据迁移依赖）

Revision ID: g7c3d4e5f6a7
Revises: g5b2c3d4e5f6
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "g7c3d4e5f6a7"
down_revision: Union[str, None] = "g5b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "knowledge_bases",
        sa.Column(
            "business_domain",
            sa.String(length=30),
            nullable=False,
            server_default="general",
        ),
    )
    op.create_index(
        "ix_knowledge_bases_business_domain",
        "knowledge_bases",
        ["business_domain"],
    )


def downgrade() -> None:
    op.drop_index("ix_knowledge_bases_business_domain", table_name="knowledge_bases")
    op.drop_column("knowledge_bases", "business_domain")
