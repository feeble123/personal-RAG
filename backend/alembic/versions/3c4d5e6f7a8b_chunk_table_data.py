"""单元二 2-2：chunks 加 table_data（表格结构化数据 JSON 列）

- table_data：可空 JSON = {table_id, columns, rows, row_index}
- 仅表格子块携带；非表格块 NULL
- 旧数据无表格结构 → NULL，兼容现有检索

Revision ID: 3c4d5e6f7a8b
Revises: 2b3c4d5e6f7a
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3c4d5e6f7a8b'
down_revision: Union[str, None] = '2b3c4d5e6f7a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'chunks',
        sa.Column('table_data', sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('chunks', 'table_data')
