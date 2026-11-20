"""待审提案支持「新实例 / 新关系」（F5 图提案）。

原来的 `ontology_proposals` 只有「实例 + 字段」的形状——`instance_uid` 指向一个
**已经存在**的实例。可建本体最费工的是新实例和新关系，这张表当时装不下。

加两列，不动任何现有列：

- `kind`：`attr`（原有行为）/ `instance` / `relation`；
- `payload`：结构化提案内容。`instance` 存 {entity_type,name,uid,props}，
  `relation` 存 {subject_uid,predicate,object_uid,props}。

**为什么不让 `instance_uid` 变可空**：改可空要重建约束和索引，而这一列的语义
其实还成立——新实例提案的 `instance_uid` 就是**它自己的** uid（由 TBox 前缀
+ 名字确定性生成），关系提案的 `instance_uid` 是主语 uid。语义没破，就没必要
冒一次重建的险。

Revision ID: n5e6f7a8b9c0
Revises: m4d5e6f7a8b9
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "n5e6f7a8b9c0"
down_revision: Union[str, None] = "m4d5e6f7a8b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _is_pg() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _json():
    return postgresql.JSONB(astext_type=sa.Text()) if _is_pg() else sa.JSON()


def upgrade() -> None:
    op.add_column(
        "ontology_proposals",
        sa.Column("kind", sa.String(length=12), server_default="attr", nullable=False),
    )
    op.add_column(
        "ontology_proposals",
        sa.Column("payload", _json(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("ontology_proposals", "payload")
    op.drop_column("ontology_proposals", "kind")
