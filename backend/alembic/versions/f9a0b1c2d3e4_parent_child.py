"""P1-4 parent-child：chunks 加 parent_chunk_id + parent_context 列

- parent_chunk_id：自引用 FK（SET NULL）+ 索引（检索注入父上下文用）
- parent_context：父块全文冗余（免 join）
- 旧数据无父块 → 两列 NULL，兼容现有检索

Revision ID: f9a0b1c2d3e4
Revises: f8a9b0c1d2e3
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f9a0b1c2d3e4'
down_revision: Union[str, None] = 'f8a9b0c1d2e3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('chunks', schema=None) as batch_op:
        batch_op.add_column(sa.Column('parent_chunk_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('parent_context', sa.Text(), nullable=True))
        batch_op.create_index('ix_chunks_parent', ['parent_chunk_id'])
        batch_op.create_foreign_key(
            'fk_chunks_parent', 'chunks', ['parent_chunk_id'], ['id'], ondelete='SET NULL'
        )


def downgrade() -> None:
    with op.batch_alter_table('chunks', schema=None) as batch_op:
        batch_op.drop_constraint('fk_chunks_parent', type_='foreignkey')
        batch_op.drop_index('ix_chunks_parent')
        batch_op.drop_column('parent_context')
        batch_op.drop_column('parent_chunk_id')
