"""P0-11 检索出处元数据：chunks 加 block_type/clause_no/formula_no，documents 加 doc_type

- 纯增量列（batch add column），全部可空/有默认，不动现有数据
- chunks.block_type: text/table/formula/figure（解析器能拿到的块类型）
- chunks.clause_no: 条款号（如 7.4.2）
- chunks.formula_no: 公式编号（如 7.4.3-1）
- documents.doc_type: textbook/standard/manual/other（上传时选择，未来 DSH 引用来源判断）

Revision ID: d6e7f8a9b0c1
Revises: c3d4e5f6a7b8
Create Date: 2026-08-22
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd6e7f8a9b0c1'
down_revision: Union[str, None] = 'c3d4e5f6a7b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('chunks', schema=None) as batch_op:
        batch_op.add_column(sa.Column('block_type', sa.String(length=10), nullable=True))
        batch_op.add_column(sa.Column('clause_no', sa.String(length=30), nullable=True))
        batch_op.add_column(sa.Column('formula_no', sa.String(length=30), nullable=True))
    with op.batch_alter_table('documents', schema=None) as batch_op:
        batch_op.add_column(sa.Column('doc_type', sa.String(length=20), nullable=False, server_default='other'))


def downgrade() -> None:
    with op.batch_alter_table('documents', schema=None) as batch_op:
        batch_op.drop_column('doc_type')
    with op.batch_alter_table('chunks', schema=None) as batch_op:
        batch_op.drop_column('formula_no')
        batch_op.drop_column('clause_no')
        batch_op.drop_column('block_type')
